"""The asyncpg-backed ``MetadataStore`` implementation.

The ``uploads`` schema is owned by Alembic (migrations/), NOT self-created here
anymore. `alembic upgrade head` runs as a dedicated step before the api and
worker start (the compose `migrate` service; wired into CI too). The store
assumes the table already exists; `connect()` just builds the pool and
fail-fast-validates the DSN. See CLAUDE.md "Non-obvious invariants".

Postgres, not SQLite, on purpose: two OS processes (api and worker) write this
store, and SQLite over a shared volume reintroduces exactly the cross-process
locking fragility the storage-singleton pattern was built to avoid.
"""

from __future__ import annotations

import asyncio
import builtins
import uuid
from typing import cast

import asyncpg

from .store import MetadataError, MetadataStore
from .types import KIND_VIDEO, STATUS_FAILED, STATUS_PROCESSING, STATUS_READY, UploadRecord

# Column order shared by every RETURNING */SELECT * below, so a Record maps to
# UploadRecord positionally without naming columns at every call site.
_COLUMNS = (
    "id, owner, kind, storage_key, content_type, size_bytes, width, height, "
    "content_hash, status, task_id, original_filename, duration_seconds, truncated, "
    "callback_url, poster_upload_id, webhook_status, webhook_attempts, webhook_last_error, "
    "webhook_updated_at, visibility, share_token, is_linked, created_at, updated_at"
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
        callback_url=row["callback_url"],
        poster_upload_id=row["poster_upload_id"],
        webhook_status=row["webhook_status"],
        webhook_attempts=row["webhook_attempts"],
        webhook_last_error=row["webhook_last_error"],
        webhook_updated_at=row["webhook_updated_at"],
        visibility=row["visibility"],
        share_token=row["share_token"],
        is_linked=row["is_linked"],
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
                        pool = await asyncpg.create_pool(
                            dsn=self._dsn,
                            min_size=1,
                            max_size=10,
                            max_inactive_connection_lifetime=300.0,
                            command_timeout=30.0,
                        )
                    except (asyncpg.PostgresError, OSError) as exc:
                        raise MetadataError("Failed to initialize metadata store") from exc
                    self._pool = pool
        return self._pool

    # -- Query helpers --------------------------------------------------
    # Every method below was "acquire the pool, run one statement, translate
    # a driver/connection failure into a sanitized MetadataError". These four
    # wrappers factor that skeleton out once so each method below states only
    # its SQL, its params, and its error message.

    async def _fetchrow(self, sql: str, *args: object, error_msg: str) -> asyncpg.Record | None:
        """Single-row query with standardized error handling."""
        pool = await self._get_pool()
        try:
            return await pool.fetchrow(sql, *args)
        except (asyncpg.PostgresError, OSError) as exc:
            raise MetadataError(error_msg) from exc

    async def _fetch(self, sql: str, *args: object, error_msg: str) -> list[asyncpg.Record]:
        """Multi-row query with standardized error handling."""
        pool = await self._get_pool()
        try:
            return await pool.fetch(sql, *args)
        except (asyncpg.PostgresError, OSError) as exc:
            raise MetadataError(error_msg) from exc

    async def _execute(self, sql: str, *args: object, error_msg: str) -> None:
        """Statement with no return value, standardized error handling."""
        pool = await self._get_pool()
        try:
            await pool.execute(sql, *args)
        except (asyncpg.PostgresError, OSError) as exc:
            raise MetadataError(error_msg) from exc

    async def _fetchval(self, sql: str, *args: object, error_msg: str) -> object:
        """Single-value query with standardized error handling."""
        pool = await self._get_pool()
        try:
            return await pool.fetchval(sql, *args)
        except (asyncpg.PostgresError, OSError) as exc:
            raise MetadataError(error_msg) from exc

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
        callback_url: str | None = None,
    ) -> UploadRecord:
        upload_id = uuid.uuid4().hex
        row = await self._fetchrow(
            f"INSERT INTO uploads (id, owner, kind, storage_key, content_type, size_bytes, "
            f"width, height, content_hash, status, task_id, original_filename, callback_url) "
            f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) "
            f"RETURNING {_COLUMNS}",
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
            callback_url,
            error_msg=f"Failed to record upload {storage_key!r}",
        )
        assert row is not None  # INSERT ... RETURNING always yields a row
        return _row_to_record(row)

    async def set_task_id(self, upload_id: str, task_id: str) -> None:
        await self._execute(
            "UPDATE uploads SET task_id = $2, updated_at = now() WHERE id = $1",
            upload_id,
            task_id,
            error_msg=f"Failed to attach task to upload {upload_id!r}",
        )

    async def get(self, upload_id: str, owner: str) -> UploadRecord | None:
        row = await self._fetchrow(
            f"SELECT {_COLUMNS} FROM uploads WHERE id = $1 AND owner = $2",
            upload_id,
            owner,
            error_msg=f"Failed to load upload {upload_id!r}",
        )
        return _row_to_record(row) if row is not None else None

    async def get_by_task_id(self, task_id: str, owner: str) -> UploadRecord | None:
        row = await self._fetchrow(
            f"SELECT {_COLUMNS} FROM uploads WHERE task_id = $1 AND owner = $2",
            task_id,
            owner,
            error_msg=f"Failed to load task {task_id!r}",
        )
        return _row_to_record(row) if row is not None else None

    async def list(
        self, owner: str, *, kind: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[UploadRecord]:
        rows = await self._fetch(
            f"SELECT {_COLUMNS} FROM uploads "
            f"WHERE owner = $1 AND ($2::text IS NULL OR kind = $2) "
            f"ORDER BY created_at DESC LIMIT $3 OFFSET $4",
            owner,
            kind,
            limit,
            offset,
            error_msg="Failed to list uploads",
        )
        return [_row_to_record(row) for row in rows]

    async def count(self, owner: str, *, kind: str | None = None) -> int:
        val = await self._fetchval(
            "SELECT COUNT(*) FROM uploads WHERE owner = $1 AND ($2::text IS NULL OR kind = $2)",
            owner,
            kind,
            error_msg="Failed to count uploads",
        )
        return cast(int, val)

    async def delete(self, upload_id: str, owner: str | None = None) -> UploadRecord | None:
        if owner is not None:
            sql = f"DELETE FROM uploads WHERE id = $1 AND owner = $2 RETURNING {_COLUMNS}"
            args: tuple[object, ...] = (upload_id, owner)
        else:
            sql = f"DELETE FROM uploads WHERE id = $1 RETURNING {_COLUMNS}"
            args = (upload_id,)
        row = await self._fetchrow(sql, *args, error_msg=f"Failed to delete upload {upload_id!r}")
        return _row_to_record(row) if row is not None else None

    async def find_ready_by_hash(self, owner: str, content_hash: str) -> UploadRecord | None:
        row = await self._fetchrow(
            f"SELECT {_COLUMNS} FROM uploads "
            f"WHERE owner = $1 AND content_hash = $2 AND status = '{STATUS_READY}' "
            f"ORDER BY created_at DESC LIMIT 1",
            owner,
            content_hash,
            error_msg="Failed to look up upload by hash",
        )
        return _row_to_record(row) if row is not None else None

    async def find_active_video_by_hash(self, owner: str, content_hash: str) -> UploadRecord | None:
        row = await self._fetchrow(
            f"SELECT {_COLUMNS} FROM uploads "
            f"WHERE owner = $1 AND content_hash = $2 AND kind = '{KIND_VIDEO}' "
            f"AND status IN ('{STATUS_READY}', '{STATUS_PROCESSING}') "
            f"ORDER BY created_at DESC LIMIT 1",
            owner,
            content_hash,
            error_msg="Failed to look up video by hash",
        )
        return _row_to_record(row) if row is not None else None

    async def mark_ready(
        self,
        upload_id: str,
        *,
        storage_key: str,
        size_bytes: int,
        duration_seconds: float | None = None,
        truncated: bool = False,
        width: int | None = None,
        height: int | None = None,
        content_type: str | None = None,
    ) -> UploadRecord | None:
        row = await self._fetchrow(
            f"UPDATE uploads SET storage_key = $2, size_bytes = $3, "
            f"duration_seconds = $4, truncated = $5, "
            f"width = COALESCE($6, width), height = COALESCE($7, height), "
            f"content_type = COALESCE($8, content_type), "
            f"status = '{STATUS_READY}', updated_at = now() "
            f"WHERE id = $1 RETURNING {_COLUMNS}",
            upload_id,
            storage_key,
            size_bytes,
            duration_seconds,
            truncated,
            width,
            height,
            content_type,
            error_msg=f"Failed to mark upload {upload_id!r} ready",
        )
        return _row_to_record(row) if row is not None else None

    async def mark_failed(self, upload_id: str) -> UploadRecord | None:
        row = await self._fetchrow(
            f"UPDATE uploads SET status = '{STATUS_FAILED}', updated_at = now() "
            f"WHERE id = $1 RETURNING {_COLUMNS}",
            upload_id,
            error_msg=f"Failed to mark upload {upload_id!r} failed",
        )
        return _row_to_record(row) if row is not None else None

    async def get_by_id(self, upload_id: str) -> UploadRecord | None:
        row = await self._fetchrow(
            f"SELECT {_COLUMNS} FROM uploads WHERE id = $1",
            upload_id,
            error_msg=f"Failed to load upload {upload_id!r}",
        )
        return _row_to_record(row) if row is not None else None

    async def set_poster(self, video_id: str, poster_upload_id: str) -> UploadRecord | None:
        row = await self._fetchrow(
            f"UPDATE uploads SET poster_upload_id = $2, updated_at = now() "
            f"WHERE id = $1 RETURNING {_COLUMNS}",
            video_id,
            poster_upload_id,
            error_msg=f"Failed to set poster on upload {video_id!r}",
        )
        return _row_to_record(row) if row is not None else None

    async def mark_webhook(
        self,
        upload_id: str,
        *,
        status: str,
        attempts: int = 0,
        last_error: str | None = None,
    ) -> UploadRecord | None:
        row = await self._fetchrow(
            f"UPDATE uploads SET webhook_status = $2, webhook_attempts = $3, "
            f"webhook_last_error = $4, webhook_updated_at = now() "
            f"WHERE id = $1 RETURNING {_COLUMNS}",
            upload_id,
            status,
            attempts,
            last_error,
            error_msg=f"Failed to record webhook state for {upload_id!r}",
        )
        return _row_to_record(row) if row is not None else None

    async def set_visibility(
        self, upload_id: str, owner: str, visibility: str
    ) -> UploadRecord | None:
        row = await self._fetchrow(
            f"UPDATE uploads SET visibility = $3, updated_at = now() "
            f"WHERE id = $1 AND owner = $2 RETURNING {_COLUMNS}",
            upload_id,
            owner,
            visibility,
            error_msg=f"Failed to set visibility on upload {upload_id!r}",
        )
        return _row_to_record(row) if row is not None else None

    async def set_share_token(self, upload_id: str, owner: str, token: str) -> UploadRecord | None:
        row = await self._fetchrow(
            f"UPDATE uploads SET share_token = $3, updated_at = now() "
            f"WHERE id = $1 AND owner = $2 RETURNING {_COLUMNS}",
            upload_id,
            owner,
            token,
            error_msg=f"Failed to set share token on upload {upload_id!r}",
        )
        return _row_to_record(row) if row is not None else None

    async def clear_share_token(self, upload_id: str, owner: str) -> UploadRecord | None:
        row = await self._fetchrow(
            f"UPDATE uploads SET share_token = NULL, updated_at = now() "
            f"WHERE id = $1 AND owner = $2 RETURNING {_COLUMNS}",
            upload_id,
            owner,
            error_msg=f"Failed to clear share token on upload {upload_id!r}",
        )
        return _row_to_record(row) if row is not None else None

    async def get_by_share_token(self, token: str) -> UploadRecord | None:
        row = await self._fetchrow(
            f"SELECT {_COLUMNS} FROM uploads WHERE share_token = $1",
            token,
            error_msg="Failed to look up upload by share token",
        )
        return _row_to_record(row) if row is not None else None

    async def mark_linked(self, upload_id: str, owner: str) -> UploadRecord | None:
        row = await self._fetchrow(
            f"UPDATE uploads SET is_linked = true, updated_at = now() "
            f"WHERE id = $1 AND owner = $2 RETURNING {_COLUMNS}",
            upload_id,
            owner,
            error_msg=f"Failed to mark upload {upload_id!r} linked",
        )
        return _row_to_record(row) if row is not None else None

    async def get_unlinked_older_than(
        self, hours: int, limit: int = 100
    ) -> builtins.list[UploadRecord]:
        rows = await self._fetch(
            f"SELECT {_COLUMNS} FROM uploads "
            f"WHERE is_linked = false AND created_at < now() - ($1 * INTERVAL '1 hour') "
            f"ORDER BY created_at ASC LIMIT $2",
            hours,
            limit,
            error_msg="Failed to fetch unlinked uploads",
        )
        return [_row_to_record(row) for row in rows]

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
