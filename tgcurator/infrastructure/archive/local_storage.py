from __future__ import annotations

import asyncio
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath


class ArchiveStorageKeyError(ValueError):
    """Raised when a caller attempts to address an archive object outside the storage root."""


class ArchiveObjectConflictError(RuntimeError):
    """Raised when an immutable archive key already names different bytes."""


class LocalArchiveStorage:
    """Local-volume archive adapter with immutable keys and atomic publication.

    The database stores only a backend name and caller-defined relative key.  This adapter rejects
    host paths, writes a fully fsynced temporary file in the target directory, then atomically
    creates the final name without replacing an existing archive object.  Repeating `put` with
    identical bytes is idempotent; different bytes for the same key are an explicit conflict.
    """

    def __init__(self, *, root: Path | str) -> None:
        self._root = Path(root).expanduser().resolve()

    async def put(self, *, key: str, content: bytes, content_type: str) -> int:
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        if not content_type.strip():
            raise ValueError("content_type must not be blank")
        return await asyncio.to_thread(self._put_sync, key, content)

    async def open(self, *, key: str) -> bytes:
        return await asyncio.to_thread(self._read_sync, key)

    async def exists(self, *, key: str) -> bool:
        return await asyncio.to_thread(self._exists_sync, key)

    async def delete(self, *, key: str) -> None:
        await asyncio.to_thread(self._delete_sync, key)

    async def size(self, *, key: str) -> int:
        return await asyncio.to_thread(self._size_sync, key)

    def _put_sync(self, key: str, content: bytes) -> int:
        target = self._target_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return self._existing_size_or_conflict(target, content)

        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=".tgcurator-archive-",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # link() creates a directory entry atomically and refuses to replace any existing
                # key.  The target therefore never becomes visible with partial bytes.
                os.link(temporary, target)
            except FileExistsError:
                return self._existing_size_or_conflict(target, content)
            self._fsync_directory(target.parent)
            return len(content)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _read_sync(self, key: str) -> bytes:
        target = self._target_for(key)
        self._require_regular_file(target)
        return target.read_bytes()

    def _exists_sync(self, key: str) -> bool:
        target = self._target_for(key)
        try:
            mode = os.lstat(target).st_mode
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(mode):
            raise ArchiveStorageKeyError("archive key does not reference a regular file")
        return True

    def _delete_sync(self, key: str) -> None:
        target = self._target_for(key)
        try:
            self._require_regular_file(target)
        except FileNotFoundError:
            return
        target.unlink()
        self._fsync_directory(target.parent)

    def _size_sync(self, key: str) -> int:
        target = self._target_for(key)
        self._require_regular_file(target)
        return target.stat().st_size

    def _existing_size_or_conflict(self, target: Path, content: bytes) -> int:
        self._require_regular_file(target)
        existing = target.read_bytes()
        if existing != content:
            raise ArchiveObjectConflictError("archive key already exists with different bytes")
        return len(existing)

    def _target_for(self, key: str) -> Path:
        if not isinstance(key, str) or not key.strip():
            raise ArchiveStorageKeyError("archive key must not be blank")
        if "\\" in key:
            raise ArchiveStorageKeyError("archive key must use relative POSIX separators")
        normalized = PurePosixPath(key)
        if normalized.is_absolute() or normalized == PurePosixPath("."):
            raise ArchiveStorageKeyError("archive key must be a non-empty relative path")
        if any(part in {"", ".", ".."} for part in normalized.parts):
            raise ArchiveStorageKeyError("archive key must not contain traversal components")
        if any(":" in part for part in normalized.parts):
            raise ArchiveStorageKeyError("archive key must not contain a drive designator")

        target = (self._root / Path(*normalized.parts)).resolve()
        try:
            target.relative_to(self._root)
        except ValueError as error:
            raise ArchiveStorageKeyError("archive key escapes storage root") from error
        return target

    @staticmethod
    def _require_regular_file(target: Path) -> None:
        mode = os.lstat(target).st_mode
        if not stat.S_ISREG(mode):
            raise ArchiveStorageKeyError("archive key does not reference a regular file")

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Best-effort directory metadata sync; Windows does not support directory handles."""
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
