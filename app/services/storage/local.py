"""Local filesystem storage backend."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import aiofiles

from app.services.storage.base import StorageBackend, StorageError, StorageNotFound, StorageObject


class LocalStorage(StorageBackend):
    def __init__(self, root: str, public_base_url: str) -> None:
        self._root = Path(root).resolve()
        self._base = public_base_url.rstrip("/")

    def _resolve(self, key: str) -> Path:
        # Guard against path traversal escaping the storage root.
        p = Path(key)
        if p.is_absolute() or key.startswith("/") or key.startswith("\\"):
            raise StorageError(f"Invalid object key outside storage root: {key!r}")
        target = (self._root / key).resolve()
        if not target.is_relative_to(self._root):
            raise StorageError(f"Invalid object key outside storage root: {key!r}")
        return target

    def public_url(self, key: str) -> str:
        safe_key = key.lstrip("/")
        return f"{self._base}/{safe_key}" if self._base else safe_key

    async def local_path(self, key: str) -> str | None:
        """The object's on-disk path, through the same traversal guard as
        read/write -- but only if it's actually there. A co-located worker
        (sharing the media volume) hands this straight to ffmpeg, so the
        bytes never round-trip through its memory; a path to a nonexistent
        file would otherwise be handed to ffmpeg as-is and fail with an
        unrelated, confusing ffmpeg error instead of a clean StorageError
        (the caller falls back to presigned_get_url, and -- local having
        none -- surfaces a clear "no input source" failure instead)."""
        target = self._resolve(key)
        if not await asyncio.to_thread(target.is_file):
            return None
        return str(target)

    async def upload(self, data: bytes, key: str, content_type: str) -> StorageObject:
        target = self._resolve(key)
        # mkdir() is a blocking syscall -- offload it, same as delete()'s
        # unlink() below (every other blocking call on this class already is).
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        async with aiofiles.open(target, "wb") as f:
            await f.write(data)
        return StorageObject(
            key=key, url=self.public_url(key), size=len(data), content_type=content_type
        )

    async def upload_from_path(self, path: str, key: str, content_type: str) -> StorageObject:
        # Stream the staged temp file into place a chunk at a time; never load the
        # whole (possibly hundreds-of-MB) object into memory.
        target = self._resolve(key)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        size = 0
        async with aiofiles.open(path, "rb") as src, aiofiles.open(target, "wb") as dst:
            while chunk := await src.read(1024 * 1024):
                await dst.write(chunk)
                size += len(chunk)
        return StorageObject(
            key=key, url=self.public_url(key), size=size, content_type=content_type
        )

    async def download(self, key: str) -> bytes:
        target = self._resolve(key)
        try:
            async with aiofiles.open(target, "rb") as f:
                return await f.read()
        except FileNotFoundError as exc:
            raise StorageNotFound(f"Object not found: {key!r}") from exc

    async def delete(self, key: str) -> None:
        # Idempotent: deleting a missing object is not an error. unlink() is a
        # blocking syscall -- offload it so it never stalls the event loop
        # (this runs on every DELETE /files/{id}, rendition cleanup, and
        # visibility-rotation cleanup, unlike every other method on this class,
        # which already goes through aiofiles).
        await asyncio.to_thread(self._resolve(key).unlink, missing_ok=True)

    async def copy(self, src_key: str, dst_key: str) -> None:
        src, dst = self._resolve(src_key), self._resolve(dst_key)
        if not await asyncio.to_thread(src.is_file):
            raise StorageNotFound(f"Object not found: {src_key!r}")
        await asyncio.to_thread(dst.parent.mkdir, parents=True, exist_ok=True)
        # shutil does the copy in C and off the event loop; never read the whole
        # object into this process just to write it back out.
        await asyncio.to_thread(shutil.copyfile, src, dst)
