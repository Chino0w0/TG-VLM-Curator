from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from tgcurator.application import Settings, get_settings
from tgcurator.application.maintenance import StorageConsistencyScanner
from tgcurator.infrastructure.archive import LocalArchiveStorage
from tgcurator.infrastructure.database import (
    AsyncDatabase,
    SqlAlchemyArchiveConsistencyRepository,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit PostgreSQL READY archive blob metadata against the local "
            "archive storage directory."
        )
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        help="Path to archive-data root (defaults to TGCURATOR_ARCHIVE_ROOT).",
    )
    parser.add_argument(
        "--storage-backend",
        default="local",
        help="Storage backend identifier recorded in PostgreSQL (default: local).",
    )
    parser.add_argument(
        "--verify-sha256",
        action="store_true",
        help="Recompute SHA-256 checksums for every READY archive file.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Emit the consistency report as JSON.",
    )
    return parser.parse_args(argv)


async def run_scan(
    *,
    settings: Settings,
    archive_root: Path | None,
    storage_backend: str = "local",
    verify_sha256: bool = False,
    output_json: bool = False,
) -> int:
    if not settings.database_url:
        raise RuntimeError("TGCURATOR_DATABASE_URL is required for consistency scan")
    resolved_root = archive_root or settings.archive_root
    if resolved_root is None:
        raise RuntimeError(
            "--archive-root or TGCURATOR_ARCHIVE_ROOT is required for consistency scan"
        )

    database = AsyncDatabase(settings.database_url)
    try:
        scanner = StorageConsistencyScanner(
            repository=SqlAlchemyArchiveConsistencyRepository(database),
            storage=LocalArchiveStorage(root=resolved_root),
            storage_backend=storage_backend,
        )
        report = await scanner.scan(
            now=datetime.now(UTC),
            verify_sha256=verify_sha256,
        )
    finally:
        await database.dispose()

    payload = report.as_dict()
    if output_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        status_label = "CONSISTENT" if report.consistent else "DISCREPANCIES_FOUND"
        print(
            f"[{status_label}] backend={report.storage_backend} "
            f"checked_ready={report.checked_ready_count} "
            f"observed_files={report.observed_file_count} "
            f"missing={len(report.missing_blobs)} "
            f"corrupted={len(report.corrupted_blobs)} "
            f"orphans={len(report.orphan_files)}"
        )
    return 0 if report.consistent else 2


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    raise SystemExit(
        asyncio.run(
            run_scan(
                settings=get_settings(),
                archive_root=args.archive_root,
                storage_backend=args.storage_backend,
                verify_sha256=args.verify_sha256,
                output_json=args.output_json,
            )
        )
    )


if __name__ == "__main__":
    main()
