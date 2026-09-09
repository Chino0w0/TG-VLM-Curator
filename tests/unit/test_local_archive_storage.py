from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from tgcurator.infrastructure.archive import (
    ArchiveObjectConflictError,
    ArchiveStorageKeyError,
    LocalArchiveStorage,
)


class LocalArchiveStorageTests(unittest.TestCase):
    def test_put_is_atomic_immutable_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            storage = LocalArchiveStorage(root=root)

            first_size = asyncio.run(
                storage.put(
                    key="source/7/message/11/image.webp",
                    content=b"webp-bytes",
                    content_type="image/webp",
                )
            )
            repeated_size = asyncio.run(
                storage.put(
                    key="source/7/message/11/image.webp",
                    content=b"webp-bytes",
                    content_type="image/webp",
                )
            )

            self.assertEqual(first_size, len(b"webp-bytes"))
            self.assertEqual(repeated_size, first_size)
            self.assertTrue(asyncio.run(storage.exists(key="source/7/message/11/image.webp")))
            self.assertEqual(
                asyncio.run(storage.open(key="source/7/message/11/image.webp")), b"webp-bytes"
            )
            self.assertEqual(
                asyncio.run(storage.size(key="source/7/message/11/image.webp")), first_size
            )
            self.assertEqual(list(root.rglob(".tgcurator-archive-*")), [])

            with self.assertRaises(ArchiveObjectConflictError):
                asyncio.run(
                    storage.put(
                        key="source/7/message/11/image.webp",
                        content=b"different-bytes",
                        content_type="image/webp",
                    )
                )

    def test_key_validation_prevents_host_path_and_traversal_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            storage = LocalArchiveStorage(root=temporary_directory)
            for invalid_key in ("", "/absolute/object", "../outside", "a/../../outside", "a\\b"):
                with self.subTest(invalid_key=invalid_key):
                    with self.assertRaises(ArchiveStorageKeyError):
                        asyncio.run(storage.exists(key=invalid_key))

            with self.assertRaises(ValueError):
                asyncio.run(storage.put(key="object", content=b"x", content_type=" "))

    def test_delete_is_idempotent_and_does_not_escape_the_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            storage = LocalArchiveStorage(root=temporary_directory)
            asyncio.run(
                storage.put(
                    key="objects/a.bin", content=b"a", content_type="application/octet-stream"
                )
            )

            asyncio.run(storage.delete(key="objects/a.bin"))
            asyncio.run(storage.delete(key="objects/a.bin"))

            self.assertFalse(asyncio.run(storage.exists(key="objects/a.bin")))


if __name__ == "__main__":
    unittest.main()
