from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from apps.runtime_dependencies import (
    check_heartbeat_file,
    resolve_database_url,
    resolve_heartbeat_file,
)
from tgcurator.application import Settings, get_settings
from tgcurator.infrastructure.database import AsyncDatabase

ServiceRole = Literal["api", "scheduler", "worker", "ingestor"]
ProbeKind = Literal["live", "ready"]
DEFAULT_PROBE_TIMEOUT_SECONDS = 3.0
DEFAULT_MAX_HEARTBEAT_AGE_SECONDS = 120.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Liveness and service-appropriate readiness probes for TG-VLM-Curator containers."
        )
    )
    parser.add_argument(
        "--role",
        choices=("api", "scheduler", "worker", "ingestor"),
        required=True,
        help="Daemon role being probed.",
    )
    parser.add_argument(
        "--probe",
        choices=("live", "ready"),
        default="ready",
        help="Probe kind: live or ready (default: ready).",
    )
    parser.add_argument(
        "--max-heartbeat-age-seconds",
        type=float,
        default=DEFAULT_MAX_HEARTBEAT_AGE_SECONDS,
        help="Maximum allowed age in seconds for local daemon/worker heartbeat file.",
    )
    return parser.parse_args(argv)


async def _default_database_probe(database_url: str) -> bool:
    database = AsyncDatabase(database_url)
    try:
        return await database.ping()
    except Exception:
        return False
    finally:
        await database.dispose()


def _sync_broker_ping(broker_url: str) -> bool:
    from kombu import Connection

    with Connection(broker_url, connect_timeout=DEFAULT_PROBE_TIMEOUT_SECONDS) as conn:
        conn.connect()
        return bool(conn.connected)


async def _default_broker_probe(broker_url: str) -> bool:
    try:
        return await asyncio.to_thread(_sync_broker_ping, broker_url)
    except Exception:
        return False


async def evaluate_service_probe(
    *,
    role: ServiceRole,
    probe: ProbeKind,
    settings: Settings,
    database_probe: Callable[[str], Awaitable[bool]] = _default_database_probe,
    broker_probe: Callable[[str], Awaitable[bool]] = _default_broker_probe,
    probe_timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    heartbeat_file: Path | None = None,
    max_heartbeat_age_seconds: float = DEFAULT_MAX_HEARTBEAT_AGE_SECONDS,
    now: datetime | None = None,
) -> tuple[int, dict[str, object]]:
    """Evaluate service-appropriate health without leaking secrets or tracebacks.

    Section 41 Invariant:
    - `api` and `ingestor` readiness check PostgreSQL only (external Telegram or
      Inference Provider outages never mark the API unready).
    - `scheduler` and `worker` readiness check both PostgreSQL and Redis/broker.
    - When a local daemon heartbeat path is configured (`TGCURATOR_HEARTBEAT_DIR` or
      `TGCURATOR_HEARTBEAT_FILE`), `scheduler`, `worker`, and `ingestor` also verify
      recent loop/process progress.
    """

    hb_blocker: str | None = None
    if role in {"scheduler", "worker", "ingestor"}:
        resolved_hb = resolve_heartbeat_file(role, explicit_path=heartbeat_file)
        if resolved_hb is not None:
            hb_ok, hb_blocker = check_heartbeat_file(
                resolved_hb,
                now=now,
                max_age_seconds=max_heartbeat_age_seconds,
            )
            if not hb_ok and hb_blocker is None:
                hb_blocker = "heartbeat_stale"

    if probe == "live":
        if hb_blocker is not None:
            return 1, {
                "status": "degraded",
                "role": role,
                "probe": "live",
                "blockers": [hb_blocker],
            }
        return 0, {"status": "alive", "role": role, "probe": "live"}

    blockers: list[str] = []
    if hb_blocker is not None:
        blockers.append(hb_blocker)

    db_ok = False
    database_url = resolve_database_url(settings)
    if not database_url:
        blockers.append("database_unconfigured")
    else:
        try:
            db_ok = await asyncio.wait_for(
                database_probe(database_url),
                timeout=probe_timeout_seconds,
            )
        except (TimeoutError, Exception):
            db_ok = False
        if not db_ok:
            blockers.append("database_unavailable")

    requires_broker = role in {"scheduler", "worker"}
    broker_ok: bool | None = None
    if requires_broker:
        broker_secret = settings.celery_broker_url
        broker_url = broker_secret.get_secret_value().strip() if broker_secret is not None else ""
        if not broker_url:
            broker_ok = False
            blockers.append("broker_unconfigured")
        else:
            try:
                broker_ok = await asyncio.wait_for(
                    broker_probe(broker_url),
                    timeout=probe_timeout_seconds,
                )
            except (TimeoutError, Exception):
                broker_ok = False
            if not broker_ok:
                blockers.append("broker_unavailable")

    ready = len(blockers) == 0
    payload: dict[str, object] = {
        "status": "ready" if ready else "degraded",
        "role": role,
        "probe": "ready",
        "database_ready": db_ok,
        "blockers": blockers,
    }
    if requires_broker:
        payload["broker_ready"] = bool(broker_ok)
    return (0 if ready else 1), payload


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    code, payload = asyncio.run(
        evaluate_service_probe(
            role=args.role,
            probe=args.probe,
            settings=get_settings(),
            max_heartbeat_age_seconds=args.max_heartbeat_age_seconds,
        )
    )
    print(json.dumps(payload, sort_keys=True))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
