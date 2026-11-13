from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import URL

from tgcurator.application.ports.cleanup import MappingArchiveStorageResolver
from tgcurator.application.ports.secrets import SecretVault
from tgcurator.application.settings import Settings
from tgcurator.infrastructure.archive import LocalArchiveStorage
from tgcurator.infrastructure.database.secret_vault import SqlAlchemySecretVault
from tgcurator.infrastructure.database.session import AsyncDatabase
from tgcurator.infrastructure.security import AesGcmSecretCipher

logger = logging.getLogger(__name__)

_INFERENCE_SECRET_TYPES: frozenset[str] = frozenset({"api_key", "inference_api_key"})

_UPSERT_HEARTBEAT_SQL = text(
    """
    INSERT INTO service_heartbeats (
        service_name,
        database_ready,
        broker_ready,
        last_heartbeat_at,
        updated_at
    ) VALUES (
        :service_name,
        :database_ready,
        :broker_ready,
        :last_heartbeat_at,
        :updated_at
    )
    ON CONFLICT (service_name) DO UPDATE SET
        database_ready = EXCLUDED.database_ready,
        broker_ready = EXCLUDED.broker_ready,
        last_heartbeat_at = GREATEST(
            service_heartbeats.last_heartbeat_at,
            EXCLUDED.last_heartbeat_at
        ),
        updated_at = EXCLUDED.updated_at
    """
)


class VaultInferenceSecretResolver:
    def __init__(
        self,
        vault: SecretVault,
        *,
        expected_secret_types: frozenset[str] = _INFERENCE_SECRET_TYPES,
    ) -> None:
        self.vault = vault
        self.expected_secret_types = expected_secret_types

    async def resolve(self, *, secret_reference: str) -> str:
        secret_id = UUID(secret_reference.strip())
        expected_type = "api_key"
        status_fn = getattr(self.vault, "status", None)
        if callable(status_fn):
            status = await status_fn(secret_id=secret_id)
            if status is not None and status.secret_type in self.expected_secret_types:
                expected_type = status.secret_type
        raw = await self.vault.resolve(
            secret_id=secret_id,
            expected_secret_type=expected_type,
        )
        return raw.decode("utf-8")


def resolve_database_url(settings: Settings) -> str | None:
    if settings.database_url is not None and settings.database_url.strip():
        return settings.database_url.strip()
    host = (os.environ.get("POSTGRES_HOST") or "").strip()
    password = os.environ.get("POSTGRES_PASSWORD")
    if not host or password is None:
        return None
    user = (os.environ.get("POSTGRES_USER") or "tgcurator").strip() or "tgcurator"
    database = (os.environ.get("POSTGRES_DB") or "tgcurator").strip() or "tgcurator"
    raw_port = (os.environ.get("POSTGRES_PORT") or "5432").strip() or "5432"
    try:
        port = int(raw_port)
    except ValueError as error:
        raise RuntimeError("POSTGRES_PORT must be an integer") from error
    return URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password,
        host=host,
        port=port,
        database=database,
    ).render_as_string(hide_password=False)


def resolve_heartbeat_file(role: str, *, explicit_path: Path | None = None) -> Path | None:
    if explicit_path is not None:
        return explicit_path
    env_file = (os.environ.get("TGCURATOR_HEARTBEAT_FILE") or "").strip()
    if env_file:
        return Path(env_file)
    env_dir = (os.environ.get("TGCURATOR_HEARTBEAT_DIR") or "").strip()
    if env_dir:
        return Path(env_dir) / f"{role}.json"
    return None


def write_heartbeat_file(
    role: str,
    *,
    status: str = "running",
    now: datetime | None = None,
    explicit_path: Path | None = None,
) -> Path | None:
    path = resolve_heartbeat_file(role, explicit_path=explicit_path)
    if path is None:
        return None
    stamped = now if now is not None else datetime.now(UTC)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        payload = {
            "role": role,
            "pid": os.getpid(),
            "status": status,
            "updated_at": stamped.isoformat(),
        }
        temp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        logger.debug("Failed to write local heartbeat file for %s", role, exc_info=True)
        return None
    return path


def clear_heartbeat_file(role: str, *, explicit_path: Path | None = None) -> None:
    path = resolve_heartbeat_file(role, explicit_path=explicit_path)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Failed to remove local heartbeat file for %s", role, exc_info=True)


def check_heartbeat_file(
    path: Path,
    *,
    now: datetime | None = None,
    max_age_seconds: float = 120.0,
) -> tuple[bool, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return False, "heartbeat_missing"
    if not isinstance(data, dict):
        return False, "heartbeat_missing"
    if data.get("status") != "running":
        return False, "heartbeat_stopped"
    updated_raw = data.get("updated_at")
    if not isinstance(updated_raw, str):
        return False, "heartbeat_missing"
    try:
        updated_at = datetime.fromisoformat(updated_raw)
    except ValueError:
        return False, "heartbeat_missing"
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    reference = now if now is not None else datetime.now(UTC)
    age_seconds = (reference - updated_at).total_seconds()
    if age_seconds > max_age_seconds:
        return False, "heartbeat_stale"
    return True, None


async def upsert_service_heartbeat(
    database: Any,
    *,
    service_name: str,
    database_ready: bool,
    broker_ready: bool,
    now: datetime,
    connect_if_idle: bool = False,
) -> bool:
    if not isinstance(database, AsyncDatabase) or not database.configured:
        return False
    if not connect_if_idle and getattr(database, "_engine", None) is None:
        return False
    try:
        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    _UPSERT_HEARTBEAT_SQL,
                    {
                        "service_name": service_name,
                        "database_ready": bool(database_ready),
                        "broker_ready": bool(broker_ready),
                        "last_heartbeat_at": now,
                        "updated_at": now,
                    },
                )
        return True
    except Exception:
        logger.debug(
            "Skipping cross-process service_heartbeats upsert for %s",
            service_name,
            exc_info=True,
        )
        return False


def configured_secret_vault(
    *, settings: Settings, database: AsyncDatabase
) -> SqlAlchemySecretVault | None:
    if settings.app_master_key is None:
        return None
    cipher = AesGcmSecretCipher(
        master_key=settings.app_master_key, key_id=settings.app_master_key_id
    )
    return SqlAlchemySecretVault(database, cipher)


def configured_archive_storage(settings: Settings) -> MappingArchiveStorageResolver | None:
    if settings.archive_root is None:
        return None
    return MappingArchiveStorageResolver({"local": LocalArchiveStorage(root=settings.archive_root)})
