"""Storage abstraction for FF.

One `StorageBackend` implementation per target (local FS, S3/R2, GCS, Backblaze
B2) selected once from ``settings.STORAGE_BACKEND`` and reused for the lifetime
of the process. Backends own their network clients so connection pools / TLS
sessions are established once rather than per request. The same singleton serves
both the web process and the TaskIQ worker, so lifecycle is driven by
``close_storage()`` (wired into FastAPI's lifespan and the worker's shutdown
event) instead of ``app.state``.

This is a package rather than a single module only because the implementations
outgrew one file; every public name is re-exported here, so
``from app.services.storage import ...`` is unchanged for callers. The singleton
(`_storage`) deliberately lives in this module -- tests pre-seed it with
``monkeypatch.setattr(storage_module, "_storage", fake)``, which only works if it
sits in the same module as ``get_storage``.
"""

from __future__ import annotations

import asyncio

from app.config import settings
from app.services.storage.base import (
    DEFAULT_CONTENT_TYPE,
    StorageBackend,
    StorageError,
    StorageNotFound,
    StorageObject,
)
from app.services.storage.gcs import GCSStorage
from app.services.storage.local import LocalStorage
from app.services.storage.s3 import B2Storage, S3Storage

__all__ = [
    "DEFAULT_CONTENT_TYPE",
    "B2Storage",
    "GCSStorage",
    "LocalStorage",
    "S3Storage",
    "StorageBackend",
    "StorageError",
    "StorageNotFound",
    "StorageObject",
    "close_storage",
    "copy_file",
    "delete_file",
    "download_file",
    "get_storage",
    "has_public_base_url",
    "public_object_url",
    "upload_file",
    "upload_file_from_path",
]


# ---------------------------------------------------------------------------
# Process-wide singleton + public API
# ---------------------------------------------------------------------------

_BACKENDS: dict[str, type[StorageBackend]] = {
    "s3": S3Storage,
    "gcp": GCSStorage,
    "b2": B2Storage,
}

_storage: StorageBackend | None = None
_storage_lock = asyncio.Lock()


def _build_backend() -> StorageBackend:
    backend = settings.STORAGE_BACKEND
    if backend == "local":
        return LocalStorage(settings.LOCAL_STORAGE_DIR, settings.LOCAL_PUBLIC_BASE_URL)
    return _BACKENDS[backend]()


async def get_storage() -> StorageBackend:
    global _storage
    if _storage is None:
        async with _storage_lock:
            if _storage is None:
                _storage = _build_backend()
    return _storage


def public_object_url(key: str) -> str:
    """The object's plain, unsigned public URL -- resolved *synchronously*.

    Exists because the sync callers that need it (``UploadRecord.to_public``,
    which serializes inside a Pydantic response model) cannot await
    ``get_storage()``. Reuses the singleton when it is already built and
    otherwise constructs a throwaway backend, which is safe precisely because
    ``_build_backend`` does no I/O and opens no client -- every backend defers
    client construction to first use. It deliberately does NOT populate the
    singleton, since that would bypass ``_storage_lock``.
    """
    backend = _storage if _storage is not None else _build_backend()
    return backend.public_url(key)


def has_public_base_url() -> bool:
    """Whether the active backend can produce a URL a client can fetch directly.

    True only for the object stores with a ``*_PUBLIC_BASE_URL`` (a CDN or public
    bucket domain that actually serves the object). **Never for local**: that
    media volume is exposed only through nginx's internal X-Accel location -- by
    design, since that is what makes ownership checks unbypassable -- so a
    ``LOCAL_PUBLIC_BASE_URL``-derived link would dead-end on a 404. ``local`` has
    no entry in the settings' backend->field map, so it is False by construction
    rather than by a special case here.
    """
    return bool(settings.active_public_base_url)


async def upload_file(
    file_data: bytes,
    object_name: str,
    content_type: str = DEFAULT_CONTENT_TYPE,
) -> StorageObject:
    backend = await get_storage()
    return await backend.upload(file_data, object_name, content_type)


async def upload_file_from_path(
    path: str,
    object_name: str,
    content_type: str = DEFAULT_CONTENT_TYPE,
) -> StorageObject:
    """Stream a staged temp file into storage without loading it into memory (the
    write-side counterpart to upload_file's bytes-in interface)."""
    backend = await get_storage()
    return await backend.upload_from_path(path, object_name, content_type)


async def download_file(object_name: str) -> bytes:
    backend = await get_storage()
    return await backend.download(object_name)


async def delete_file(object_name: str) -> None:
    backend = await get_storage()
    await backend.delete(object_name)


async def copy_file(src_key: str, dst_key: str) -> None:
    backend = await get_storage()
    await backend.copy(src_key, dst_key)


async def close_storage() -> None:
    """Release backend clients. Safe to call when no backend was ever built."""
    global _storage
    if _storage is not None:
        await _storage.aclose()
        _storage = None
