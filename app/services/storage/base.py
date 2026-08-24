"""Backend-agnostic storage types and the `StorageBackend` interface.

Split out of the former single-module `app/services/storage.py`; every name here
is re-exported from the package `__init__`, so `from app.services.storage import
StorageError` (and friends) keeps resolving exactly as before.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import aiofiles


class StorageError(Exception):
    """Backend-agnostic storage failure. Never carries provider internals to callers."""


class StorageNotFound(StorageError):
    """The object is not there -- distinct from "the backend is unreachable".

    Callers need to tell these apart: a record whose object is already gone is a
    real, reachable state (DELETE removes the object before the row, so a failed
    row-delete leaves one behind), and operations like a key rotation should skip
    rather than fail when there is nothing to copy. A backend that is simply down
    must still surface as a 502.
    """


@dataclass(frozen=True)
class StorageObject:
    """Result of a successful upload."""

    key: str
    url: str
    size: int
    content_type: str


DEFAULT_CONTENT_TYPE = "application/octet-stream"


def storage_prefix(base_prefix: str, visibility: str) -> str:
    """Return the storage key prefix for a given asset type and visibility.

    Private objects are placed under the `private/` root prefix (e.g. `private/images`,
    `private/raw/videos`), establishing a path-based security boundary that allows static
    file serving and CDN delivery to safely exclude private media by path.
    """
    clean = base_prefix.strip("/")
    if visibility == "private":
        return f"private/{clean}" if not clean.startswith("private/") else clean
    return clean.removeprefix("private/")


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------


class StorageBackend(ABC):
    @abstractmethod
    async def upload(self, data: bytes, key: str, content_type: str) -> StorageObject: ...

    @abstractmethod
    async def download(self, key: str) -> bytes: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    def public_url(self, key: str) -> str:
        """The object's plain (unsigned) URL, i.e. what upload() sets on
        StorageObject.url. Lets callers that hold only a key (e.g. serving a
        deduplicated record) rebuild the same source URL without re-uploading."""

    async def aclose(self) -> None:  # noqa: B027
        """Release any long-lived clients. Default is a no-op."""

    async def presigned_get_url(  # noqa: B027
        self,
        key: str,
        expires_in: int = 3600,
        *,
        content_type: str | None = None,
        content_disposition: str | None = None,
    ) -> str | None:
        """Temporary signed GET URL, for backends where the plain object URL
        isn't usable as-is (e.g. a private S3 bucket). None means the
        backend doesn't support presigning -- callers should fall back to
        the object's regular url.

        ``content_type``/``content_disposition`` ask the store to override those
        response headers, which makes the *record* authoritative for them rather
        than whatever metadata the object happens to carry. That matters in two
        real cases: a backend migration (rclone infers Content-Type from the file
        extension, and the `local` backend has no content-type metadata to carry
        over at all -- a video landing as application/octet-stream downloads
        instead of playing), and filename preservation (the local byte paths set
        an inline Content-Disposition, so without this the object-store 302
        silently drops the original filename)."""
        pass

    async def local_path(self, key: str) -> str | None:  # NOSONAR
        """The object's path on a filesystem the caller shares, if any
        (LocalStorage) *and the object actually exists there*, else None.
        Lets a co-located worker hand ffmpeg the path directly instead of
        downloading the bytes into RAM. Object stores have no shared
        filesystem -> always None.

        Async (unlike most of this ABC's cheap accessors) because
        LocalStorage's override has to stat() the file to answer "does it
        exist", and that syscall must not block the event loop -- the one
        caller (`_resolve_ffmpeg_input`) already awaits everything else on
        this path (presigned_get_url is async too), so this costs nothing."""
        return None

    async def upload_from_path(self, path: str, key: str, content_type: str) -> StorageObject:
        """Upload the file at ``path`` to ``key``. Default reads it whole and
        delegates to upload(); backends that can stream from disk (local/s3/gcp)
        override this so a large object never lands fully in memory -- the
        write-side counterpart to the worker reading its input in place."""
        async with aiofiles.open(path, "rb") as f:
            data = await f.read()
        return await self.upload(data, key, content_type)

    async def copy(self, src_key: str, dst_key: str) -> None:
        """Copy an object within the same backend.

        Used to rotate an object's key when a record turns private, which is what
        makes already-cached public URLs dead rather than merely unadvertised.
        The default pulls the bytes through this process; every real backend
        overrides it with a server-side copy, so a multi-hundred-MB video is
        never moved through here."""
        data = await self.download(src_key)
        await self.upload(data, dst_key, DEFAULT_CONTENT_TYPE)
