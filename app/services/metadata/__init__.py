"""Metadata store: the system-of-record for every object the API creates.

Without this, uploads are fire-and-forget -- there's no way to answer "what did
owner X upload", to delete a specific object through the API, or to dedupe a
re-upload. One row per uploaded object (see the ``uploads`` schema below);
images land ``ready`` immediately, videos start ``processing`` and are flipped
to ``ready``/``failed`` by the worker when compression finishes.

Mirrors ``app/services/storage``'s design deliberately: an abstract
``MetadataStore`` with a real (Postgres/asyncpg) implementation and an
in-memory fake for tests, plus a per-process singleton driven by
``get_metadata_store()`` / ``close_metadata_store()`` and wired into the same
FastAPI-lifespan / worker-shutdown hooks as storage. Both the api and worker
processes get their own pool; the worker is the one that marks a video ready.
The ``uploads`` schema is owned by Alembic (see migrations/), applied by a
dedicated ``migrate`` step before either process starts -- the store no longer
self-creates it.

This package is split by concern (was one 668-line module):

- ``types``    -- the ``UploadRecord`` dataclass and the string constants.
- ``store``    -- the abstract ``MetadataStore`` interface and ``MetadataError``.
- ``postgres`` -- the asyncpg-backed ``PostgresMetadataStore``.

This ``__init__`` re-exports every public name so ``from app.services.metadata
import ...`` keeps working unchanged, and owns the process-wide singleton.
"""

from __future__ import annotations

import asyncio

from app.config import settings

from .postgres import PostgresMetadataStore
from .store import MetadataError, MetadataStore
from .types import (
    KIND_FILE,
    KIND_IMAGE,
    KIND_VIDEO,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_READY,
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    WEBHOOK_DELIVERED,
    WEBHOOK_FAILED,
    WEBHOOK_PENDING,
    UploadRecord,
)

__all__ = [
    "KIND_FILE",
    "KIND_IMAGE",
    "KIND_VIDEO",
    "STATUS_FAILED",
    "STATUS_PROCESSING",
    "STATUS_READY",
    "VISIBILITY_PRIVATE",
    "VISIBILITY_PUBLIC",
    "WEBHOOK_DELIVERED",
    "WEBHOOK_FAILED",
    "WEBHOOK_PENDING",
    "MetadataError",
    "MetadataStore",
    "PostgresMetadataStore",
    "UploadRecord",
    "close_metadata_store",
    "get_metadata_store",
]


# ---------------------------------------------------------------------------
# Process-wide singleton + public API (mirrors storage.py)
# ---------------------------------------------------------------------------

_store: MetadataStore | None = None
_store_lock = asyncio.Lock()


async def get_metadata_store() -> MetadataStore:
    global _store
    if _store is None:
        async with _store_lock:
            if _store is None:
                _store = PostgresMetadataStore(settings.DATABASE_URL)
    return _store


async def close_metadata_store() -> None:
    """Release the store's pool. Safe to call when no store was ever built."""
    global _store
    if _store is not None:
        await _store.aclose()
        _store = None
