"""Metadata store: the system-of-record for every object the API creates.

Without this, uploads are fire-and-forget -- there's no way to answer "what did
owner X upload", to delete a specific object through the API, or to dedupe a
re-upload. One row per uploaded object (see the ``uploads`` schema below);
images land ``ready`` immediately, videos start ``processing`` and are flipped
to ``ready``/``failed`` by the worker when compression finishes.

Mirrors ``app/services/storage.py``'s design deliberately: an abstract
``MetadataStore`` with a real (Postgres/asyncpg) implementation and an
in-memory fake for tests, plus a per-process singleton driven by
``get_metadata_store()`` / ``close_metadata_store()`` and wired into the same
FastAPI-lifespan / worker-shutdown hooks as storage. Both the api and worker
processes get their own pool; the worker is the one that marks a video ready.
The ``uploads`` schema is owned by Alembic (see migrations/), applied by a
dedicated ``migrate`` step before either process starts -- the store no longer
self-creates it.

Postgres, not SQLite, on purpose: two OS processes (api and worker) write this
store, and SQLite over a shared volume reintroduces exactly the cross-process
locking fragility the storage-singleton pattern was built to avoid.
"""

from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from app.config import settings


class MetadataError(Exception):
    """Backend-agnostic metadata-store failure. Never carries DB internals to callers."""


# Allowed record states, kept here so callers don't sprinkle string literals.
STATUS_READY = "ready"
STATUS_PROCESSING = "processing"
STATUS_FAILED = "failed"

KIND_IMAGE = "image"
KIND_VIDEO = "video"


@dataclass(frozen=True)
class UploadRecord:
    """One row of the ``uploads`` table."""

    id: str
    owner: str
    kind: str
    storage_key: str
    content_type: str
    size_bytes: int
    width: int | None
    height: int | None
    status: str
    content_hash: str | None
    task_id: str | None
    original_filename: str | None
    duration_seconds: float | None
    truncated: bool
    created_at: datetime
    updated_at: datetime

    def to_public(self) -> dict[str, Any]:
        """Owner-safe JSON view for API responses (no cross-tenant fields)."""
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "storage_key": self.storage_key,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "content_hash": self.content_hash,
            "task_id": self.task_id,
            "original_filename": self.original_filename,
            "duration_seconds": self.duration_seconds,
            "truncated": self.truncated,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# Store interface
# ---------------------------------------------------------------------------


class MetadataStore(ABC):
    @abstractmethod
    async def connect(self) -> None:
        """Establish the backend (build the pool / fail-fast on a bad DSN).
        Idempotent. The schema is owned by Alembic, not created here."""

    @abstractmethod
    async def ping(self) -> bool:
        """Cheap liveness probe for /readyz."""

    @abstractmethod
    async def create(
        self,
        *,
        owner: str,
        kind: str,
        storage_key: str,
        content_type: str,
        size_bytes: int,
        status: str,
        width: int | None = None,
        height: int | None = None,
        content_hash: str | None = None,
        task_id: str | None = None,
        original_filename: str | None = None,
    ) -> UploadRecord: ...

    @abstractmethod
    async def set_task_id(self, upload_id: str, task_id: str) -> None:
        """Attach the compression task id to a just-created video row. Called
        after enqueue (the id only exists then); a missing row is a no-op."""

    @abstractmethod
    async def get(self, upload_id: str, owner: str) -> UploadRecord | None: ...

    @abstractmethod
    async def list(
        self, owner: str, *, kind: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[UploadRecord]: ...

    @abstractmethod
    async def delete(self, upload_id: str, owner: str) -> UploadRecord | None:
        """Delete the row (owner-scoped) and return it, or None if absent."""

    @abstractmethod
    async def find_ready_by_hash(self, owner: str, content_hash: str) -> UploadRecord | None: ...

    @abstractmethod
    async def find_active_video_by_hash(self, owner: str, content_hash: str) -> UploadRecord | None:
        """Latest video record for this owner+hash that is still usable -- `ready`
        or still `processing` -- for video-upload idempotency. Excludes `failed`
        rows so a previously-failed identical input can be retried, and a
        still-`processing` hit lets a duplicate upload attach to the in-flight
        job instead of compressing the same bytes twice."""

    @abstractmethod
    async def mark_ready(
        self,
        upload_id: str,
        *,
        storage_key: str,
        size_bytes: int,
        duration_seconds: float | None = None,
        truncated: bool = False,
    ) -> UploadRecord | None:
        """Flip a processing row to ready. None means the row is gone (deleted
        mid-flight) -- the caller owns cleaning up whatever it just wrote.
        `duration_seconds`/`truncated` are the worker's ffprobe findings for a
        video (the input's duration and whether the output was capped)."""

    @abstractmethod
    async def mark_failed(self, upload_id: str) -> UploadRecord | None: ...

    async def aclose(self) -> None:  # noqa: B027 -- intentional no-op default, not abstract
        """Release any pooled clients. Default is a no-op."""


# ---------------------------------------------------------------------------
# Postgres backend (asyncpg)
# ---------------------------------------------------------------------------

# The `uploads` schema is owned by Alembic (migrations/), NOT self-created here
# anymore. `alembic upgrade head` runs as a dedicated step before the api and
# worker start (the compose `migrate` service; wired into CI too). The store
# assumes the table already exists; `connect()` just builds the pool and
# fail-fast-validates the DSN. See CLAUDE.md "Non-obvious invariants".

# Column order shared by every RETURNING */SELECT * below, so a Record maps to
# UploadRecord positionally without naming columns at every call site.
_COLUMNS = (
    "id, owner, kind, storage_key, content_type, size_bytes, width, height, "
    "content_hash, status, task_id, original_filename, duration_seconds, truncated, "
    "created_at, updated_at"
)


def _row_to_record(row: asyncpg.Record) -> UploadRecord:
    return UploadRecord(
        id=row["id"],
        owner=row["owner"],
        kind=row["kind"],
        storage_key=row["storage_key"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        width=row["width"],
        height=row["height"],
        status=row["status"],
        content_hash=row["content_hash"],
        task_id=row["task_id"],
        original_filename=row["original_filename"],
        duration_seconds=row["duration_seconds"],
        truncated=row["truncated"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PostgresMetadataStore(MetadataStore):
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> asyncpg.Pool:
        # Build the pool once, lazily. The schema itself is owned by Alembic and
        # created by the `migrate` step before either process starts, so nothing
        # is executed against the DB here beyond opening the pool.
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    try:
                        pool = await asyncpg.create_pool(dsn=self._dsn, min_size=1, max_size=10)
                    except (asyncpg.PostgresError, OSError) as exc:
                        raise MetadataError("Failed to initialize metadata store") from exc
                    self._pool = pool
        return self._pool

    async def connect(self) -> None:
        await self._get_pool()

    async def ping(self) -> bool:
        pool = await self._get_pool()
        try:
            async with pool.acquire() as conn:
                return await conn.fetchval("SELECT 1") == 1
        except (asyncpg.PostgresError, OSError) as exc:
            raise MetadataError("Metadata store ping failed") from exc

    async def create(
        self,
        *,
        owner: str,
        kind: str,
        storage_key: str,
        content_type: str,
        size_bytes: int,
        status: str,
        width: int | None = None,
        height: int | None = None,
        content_hash: str | None = None,
        task_id: str | None = None,
        original_filename: str | None = None,
    ) -> UploadRecord:
        pool = await self._get_pool()
        upload_id = uuid.uuid4().hex
        try:
            row = await pool.fetchrow(
                f"INSERT INTO uploads (id, owner, kind, storage_key, content_type, "
                f"size_bytes, width, height, content_hash, status, task_id, original_filename) "
                f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) RETURNING {_COLUMNS}",
                upload_id,
                owner,
                kind,
                storage_key,
                content_type,
                size_bytes,
                width,
                height,
                content_hash,
                status,
                task_id,
                original_filename,
            )
        except (asyncpg.PostgresError, OSError) as exc:
            raise MetadataError(f"Failed to record upload {storage_key!r}") from exc
        return _row_to_record(row)

    async def set_task_id(self, upload_id: str, task_id: str) -> None:
        pool = await self._get_pool()
        try:
            await pool.execute(
                "UPDATE uploads SET task_id = $2, updated_at = now() WHERE id = $1",
                upload_id,
                task_id,
            )
        except (asyncpg.PostgresError, OSError) as exc:
            raise MetadataError(f"Failed to attach task to upload {upload_id!r}") from exc

    async def get(self, upload_id: str, owner: str) -> UploadRecord | None:
        pool = await self._get_pool()
        try:
            row = await pool.fetchrow(
                f"SELECT {_COLUMNS} FROM uploads WHERE id = $1 AND owner = $2",
                upload_id,
                owner,
            )
        except (asyncpg.PostgresError, OSError) as exc:
            raise MetadataError(f"Failed to load upload {upload_id!r}") from exc
        return _row_to_record(row) if row is not None else None

    async def list(
        self, owner: str, *, kind: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[UploadRecord]:
        pool = await self._get_pool()
        try:
            rows = await pool.fetch(
                f"SELECT {_COLUMNS} FROM uploads "
                f"WHERE owner = $1 AND ($2::text IS NULL OR kind = $2) "
                f"ORDER BY created_at DESC LIMIT $3 OFFSET $4",
                owner,
                kind,
                limit,
                offset,
            )
        except (asyncpg.PostgresError, OSError) as exc:
            raise MetadataError("Failed to list uploads") from exc
        return [_row_to_record(row) for row in rows]

    async def delete(self, upload_id: str, owner: str) -> UploadRecord | None:
        pool = await self._get_pool()
        try:
            row = await pool.fetchrow(
                f"DELETE FROM uploads WHERE id = $1 AND owner = $2 RETURNING {_COLUMNS}",
                upload_id,
                owner,
            )
        except (asyncpg.PostgresError, OSError) as exc:
            raise MetadataError(f"Failed to delete upload {upload_id!r}") from exc
        return _row_to_record(row) if row is not None else None

    async def find_ready_by_hash(self, owner: str, content_hash: str) -> UploadRecord | None:
        pool = await self._get_pool()
        try:
            row = await pool.fetchrow(
                f"SELECT {_COLUMNS} FROM uploads "
                f"WHERE owner = $1 AND content_hash = $2 AND status = '{STATUS_READY}' "
                f"ORDER BY created_at DESC LIMIT 1",
                owner,
                content_hash,
            )
        except (asyncpg.PostgresError, OSError) as exc:
            raise MetadataError("Failed to look up upload by hash") from exc
        return _row_to_record(row) if row is not None else None

    async def find_active_video_by_hash(self, owner: str, content_hash: str) -> UploadRecord | None:
        pool = await self._get_pool()
        try:
            row = await pool.fetchrow(
                f"SELECT {_COLUMNS} FROM uploads "
                f"WHERE owner = $1 AND content_hash = $2 AND kind = '{KIND_VIDEO}' "
                f"AND status IN ('{STATUS_READY}', '{STATUS_PROCESSING}') "
                f"ORDER BY created_at DESC LIMIT 1",
                owner,
                content_hash,
            )
        except (asyncpg.PostgresError, OSError) as exc:
            raise MetadataError("Failed to look up video by hash") from exc
        return _row_to_record(row) if row is not None else None

    async def mark_ready(
        self,
        upload_id: str,
        *,
        storage_key: str,
        size_bytes: int,
        duration_seconds: float | None = None,
        truncated: bool = False,
    ) -> UploadRecord | None:
        pool = await self._get_pool()
        try:
            row = await pool.fetchrow(
                f"UPDATE uploads SET storage_key = $2, size_bytes = $3, "
                f"duration_seconds = $4, truncated = $5, "
                f"status = '{STATUS_READY}', updated_at = now() "
                f"WHERE id = $1 RETURNING {_COLUMNS}",
                upload_id,
                storage_key,
                size_bytes,
                duration_seconds,
                truncated,
            )
        except (asyncpg.PostgresError, OSError) as exc:
            raise MetadataError(f"Failed to mark upload {upload_id!r} ready") from exc
        return _row_to_record(row) if row is not None else None

    async def mark_failed(self, upload_id: str) -> UploadRecord | None:
        pool = await self._get_pool()
        try:
            row = await pool.fetchrow(
                f"UPDATE uploads SET status = '{STATUS_FAILED}', updated_at = now() "
                f"WHERE id = $1 RETURNING {_COLUMNS}",
                upload_id,
            )
        except (asyncpg.PostgresError, OSError) as exc:
            raise MetadataError(f"Failed to mark upload {upload_id!r} failed") from exc
        return _row_to_record(row) if row is not None else None

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


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
