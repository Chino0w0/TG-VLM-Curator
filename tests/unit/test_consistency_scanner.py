from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from tgcurator.application.maintenance import StorageConsistencyScanner
from tgcurator.domain.archive import (
    ExpectedArchiveBlob,
    ObservedStorageFile,
    evaluate_storage_consistency,
)
from tgcurator.infrastructure.archive import LocalArchiveStorage

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


class FakeConsistencyRepository:
    def __init__(self, records: list[ExpectedArchiveBlob]) -> None:
        self._records = list(records)

    async def list_archive_blob_records(
        self,
        *,
        storage_backend: str,
    ) -> tuple[ExpectedArchiveBlob, ...]:
        return tuple(rec for rec in self._records if rec.storage_backend == storage_backend)


class StorageConsistencyScannerTests(unittest.TestCase):
    def test_evaluate_consistent_ready_and_deleting_assets(self) -> None:
        img_bytes = b"webp-image-payload"
        frame_bytes = b"webp-frame-payload"
        records = [
            ExpectedArchiveBlob(
                entity_kind="image_asset",
                entity_id="img-1",
                storage_backend="local",
                storage_key="images/2026/11/img-1.webp",
                archive_state="ready",
                expected_size_bytes=len(img_bytes),
                expected_sha256=hashlib.sha256(img_bytes).hexdigest(),
            ),
            ExpectedArchiveBlob(
                entity_kind="video_frame",
                entity_id="vf-1",
                storage_backend="local",
                storage_key="videos/2026/11/vf-1.webp",
                archive_state="deleting",
                expected_size_bytes=len(frame_bytes),
                expected_sha256=hashlib.sha256(frame_bytes).hexdigest(),
            ),
        ]
        observed = [
            ObservedStorageFile(
                storage_key="images/2026/11/img-1.webp",
                size_bytes=len(img_bytes),
                sha256=hashlib.sha256(img_bytes).hexdigest(),
            ),
            ObservedStorageFile(
                storage_key="videos/2026/11/vf-1.webp",
                size_bytes=len(frame_bytes),
                sha256=hashlib.sha256(frame_bytes).hexdigest(),
            ),
        ]

        report = evaluate_storage_consistency(
            scanned_at=NOW,
            storage_backend="local",
            database_records=records,
            observed_files=observed,
            verify_sha256=True,
        )
        self.assertTrue(report.consistent)
        self.assertEqual(report.checked_ready_count, 1)
        self.assertEqual(report.observed_file_count, 2)
        self.assertEqual(report.missing_blobs, ())
        self.assertEqual(report.corrupted_blobs, ())
        self.assertEqual(report.orphan_files, ())

    def test_detects_missing_corrupted_and_orphan_files(self) -> None:
        records = [
            ExpectedArchiveBlob(
                entity_kind="image_asset",
                entity_id="img-missing",
                storage_backend="local",
                storage_key="images/missing.webp",
                archive_state="ready",
                expected_size_bytes=120,
            ),
            ExpectedArchiveBlob(
                entity_kind="image_asset",
                entity_id="img-size-bad",
                storage_backend="local",
                storage_key="images/size-bad.webp",
                archive_state="ready",
                expected_size_bytes=100,
            ),
            ExpectedArchiveBlob(
                entity_kind="video_frame",
                entity_id="vf-hash-bad",
                storage_backend="local",
                storage_key="videos/hash-bad.webp",
                archive_state="ready",
                expected_size_bytes=5,
                expected_sha256="a" * 64,
            ),
            ExpectedArchiveBlob(
                entity_kind="image_asset",
                entity_id="img-deleted",
                storage_backend="local",
                storage_key="images/leftover-deleted.webp",
                archive_state="deleted",
                expected_size_bytes=None,
            ),
        ]
        observed = [
            ObservedStorageFile(storage_key="images/size-bad.webp", size_bytes=77),
            ObservedStorageFile(
                storage_key="videos/hash-bad.webp",
                size_bytes=5,
                sha256="b" * 64,
            ),
            ObservedStorageFile(
                storage_key="images/leftover-deleted.webp",
                size_bytes=42,
            ),
            ObservedStorageFile(
                storage_key="untracked/stray.webp",
                size_bytes=19,
            ),
        ]

        report = evaluate_storage_consistency(
            scanned_at=NOW,
            storage_backend="local",
            database_records=records,
            observed_files=observed,
            verify_sha256=True,
        )
        self.assertFalse(report.consistent)
        self.assertEqual(len(report.missing_blobs), 1)
        self.assertEqual(report.missing_blobs[0].entity_id, "img-missing")
        self.assertEqual(
            {(item.entity_id, item.reason) for item in report.corrupted_blobs},
            {("img-size-bad", "size_mismatch"), ("vf-hash-bad", "sha256_mismatch")},
        )
        self.assertEqual(
            [item.storage_key for item in report.orphan_files],
            ["images/leftover-deleted.webp", "untracked/stray.webp"],
        )
        as_dict = report.as_dict()
        self.assertFalse(as_dict["consistent"])
        self.assertEqual(as_dict["missing_count"], 1)
        self.assertEqual(as_dict["corrupted_count"], 2)
        self.assertEqual(as_dict["orphan_count"], 2)

    def test_local_archive_storage_scanner_ignores_staging_tmp_files(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                storage = LocalArchiveStorage(root=root)
                payload = b"normalized-webp-data"
                await storage.put(
                    key="images/item-01.webp",
                    content=payload,
                    content_type="image/webp",
                )
                # Write an orphan file and a staging .tmp file
                orphan_path = root / "images" / "orphan.webp"
                orphan_path.write_bytes(b"stray")
                staging_tmp = root / "images" / ".tgcurator-archive-abc123.tmp"
                staging_tmp.write_bytes(b"partial")

                repo = FakeConsistencyRepository(
                    [
                        ExpectedArchiveBlob(
                            entity_kind="image_asset",
                            entity_id="asset-01",
                            storage_backend="local",
                            storage_key="images/item-01.webp",
                            archive_state="ready",
                            expected_size_bytes=len(payload),
                            expected_sha256=hashlib.sha256(payload).hexdigest(),
                        )
                    ]
                )
                scanner = StorageConsistencyScanner(
                    repository=repo,
                    storage=storage,
                    storage_backend="local",
                )
                report = await scanner.scan(now=NOW, verify_sha256=True)
                self.assertFalse(report.consistent)
                self.assertEqual(report.missing_blobs, ())
                self.assertEqual(report.corrupted_blobs, ())
                self.assertEqual(
                    [item.storage_key for item in report.orphan_files],
                    ["images/orphan.webp"],
                )

                # Removing orphan restores full consistency
                orphan_path.unlink()
                clean_report = await scanner.scan(now=NOW, verify_sha256=True)
                self.assertTrue(clean_report.consistent)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
