"""Test doubles for app.services.storage.StorageBackend and metadata.MetadataStore."""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import quote

from app.services.metadata import (
    KIND_VIDEO,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_READY,
    MetadataStore,
    UploadRecord,
)
from app.services.storage import StorageBackend, StorageNotFound, StorageObject


class InMemoryStorageBackend(StorageBackend):
    """Dict-backed fake. Can simulate either an S3-like (presigning-capable)
    or a local/GCS-like (non-presigning) backend via `presign_capable`.
    """

    def __init__(
        self, base_url: str = "http://fake-storage", presign_capable: bool = False
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._presign_capable = presign_capable
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.deleted_keys: list[str] = []
        self.closed = False
        self._materialized: dict[str, str] = {}

    async def upload(self, data: bytes, key: str, content_type: str) -> StorageObject:
        self.objects[key] = data
        self.content_types[key] = content_type
        return StorageObject(
            key=key, url=f"{self._base_url}/{key}", size=len(data), content_type=content_type
        )

    async def download(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError as exc:
            raise StorageNotFound(f"Object not found: {key!r}") from exc

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)
        self.deleted_keys.append(key)

    def public_url(self, key: str) -> str:
        return f"{self._base_url}/{key}"

    async def local_path(self, key: str) -> str | None:
        # Model the local backend: hand back a real on-disk path (materialized
        # from the stored bytes on first use) so a co-located worker can read
        # bytes in place, mirroring LocalStorage. Object-store backends
        # (presign_capable) have no shared filesystem -> None, so the worker's
        # resolver falls through to presigned_get_url instead. `key not in
        # self.objects` mirrors LocalStorage's own existence check.
        if self._presign_capable or key not in self.objects:
            return None
        path = self._materialized.get(key)
        if path is None:
            fd, path = tempfile.mkstemp(prefix="fakestore_")
            with os.fdopen(fd, "wb") as f:
                f.write(self.objects[key])
            self._materialized[key] = path
        return path

    async def presigned_get_url(
        self,
        key: str,
        expires_in: int = 3600,
        *,
        content_type: str | None = None,
        content_disposition: str | None = None,
    ) -> str | None:
        if not self._presign_capable:
            return None
        url = f"{self._base_url}/{key}?X-Amz-Signature=fake&expires={expires_in}"
        # Mirror the real backends: the response-header overrides are part of the
        # signed URL, so a test can assert the record (not the object's metadata)
        # is what decides Content-Type and filename.
        if content_type:
            url += f"&response-content-type={quote(content_type, safe='')}"
        if content_disposition:
            url += f"&response-content-disposition={quote(content_disposition, safe='')}"
        return url

    def cleanup(self) -> None:
        """Remove any temp files materialized by local_path (call on teardown)."""
        for path in self._materialized.values():
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
        self._materialized.clear()

    async def aclose(self) -> None:
        self.closed = True
        self.cleanup()


class InMemoryMetadataStore(MetadataStore):
    """Dict-backed fake mirroring PostgresMetadataStore's observable behavior,
    including the delete-during-processing race (mark_ready returns None when
    the row is gone). Insertion order stands in for created_at ordering so
    listing is deterministic regardless of clock resolution.
    """

    def __init__(self) -> None:
        self.records: dict[str, UploadRecord] = {}
        self._order: list[str] = []
        self.connected = False
        self.closed = False
        self._counter = 0

    async def connect(self) -> None:
        self.connected = True

    async def ping(self) -> bool:
        return True

    def _joined(self, record: UploadRecord) -> UploadRecord:
        """Resolve `poster_storage_key` fresh from the current poster row,
        mirroring PostgresMetadataStore's `LEFT JOIN uploads p ON
        u.poster_upload_id = p.id` -- it's a *join*, computed on read, never a
        persisted column, so `set_poster` must not snapshot it once and
        `self.records` must never carry a stale copy. Apply only at the same
        call sites the real store's `_JOIN_COLUMNS` queries use (`get`,
        `get_by_task_id`, `list`, `get_many`, `find_ready_by_hash`,
        `find_active_video_by_hash`, `get_by_id`, `set_poster` (via
        `get_by_id` in Postgres), `get_by_share_token`) -- the others
        (`create`, `delete`, `mark_ready`, `mark_failed`, `mark_webhook`,
        `set_visibility`, `set_share_token`, `clear_share_token`) use plain
        `_COLUMNS` in Postgres and genuinely don't carry poster info on their
        return value either.
        """
        if record.poster_upload_id is None:
            return record
        poster = self.records.get(record.poster_upload_id)
        return replace(record, poster_storage_key=poster.storage_key if poster else None)

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
        visibility: str = "private",
        **kwargs: object,
    ) -> UploadRecord:
        callback_url = kwargs.get("callback_url")
        renditions = kwargs.get("renditions")
        self._counter += 1
        upload_id = f"rec-{self._counter:08d}"
        now = datetime.now(UTC)
        record = UploadRecord(
            id=upload_id,
            owner=owner,
            kind=kind,
            storage_key=storage_key,
            content_type=content_type,
            size_bytes=size_bytes,
            width=width,
            height=height,
            status=status,
            content_hash=content_hash,
            task_id=task_id,
            original_filename=original_filename,
            duration_seconds=None,
            truncated=False,
            callback_url=callback_url,
            poster_upload_id=None,
            webhook_status=None,
            webhook_attempts=0,
            webhook_last_error=None,
            webhook_updated_at=None,
            visibility=visibility,
            share_token=None,
            created_at=now,
            updated_at=now,
            renditions=renditions,
        )
        self.records[upload_id] = record
        self._order.append(upload_id)
        return record

    async def set_task_id(self, upload_id: str, task_id: str) -> None:
        record = self.records.get(upload_id)
        if record is not None:
            self.records[upload_id] = replace(record, task_id=task_id, updated_at=datetime.now(UTC))

    async def get(self, upload_id: str, owner: str) -> UploadRecord | None:
        record = self.records.get(upload_id)
        if record is None or record.owner != owner:
            return None
        return self._joined(record)

    async def get_by_task_id(self, task_id: str, owner: str) -> UploadRecord | None:
        for i in reversed(self._order):
            record = self.records[i]
            if record.task_id == task_id and record.owner == owner:
                return self._joined(record)
        return None

    async def list(
        self,
        owner: str,
        *,
        kind: str | None = None,
        status: str | None = None,
        visibility: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[UploadRecord]:
        matches = [
            self._joined(self.records[i])
            for i in reversed(self._order)
            if self.records[i].owner == owner
            and (kind is None or self.records[i].kind == kind)
            and (status is None or self.records[i].status == status)
            and (visibility is None or self.records[i].visibility == visibility)
        ]
        return matches[offset : offset + limit]

    async def count(
        self,
        owner: str,
        *,
        kind: str | None = None,
        status: str | None = None,
        visibility: str | None = None,
    ) -> int:
        return len(
            [
                r
                for r in self.records.values()
                if r.owner == owner
                and (kind is None or r.kind == kind)
                and (status is None or r.status == status)
                and (visibility is None or r.visibility == visibility)
            ]
        )

    async def get_many(self, owner: str, upload_ids: Sequence[str]) -> Sequence[UploadRecord]:
        wanted = set(upload_ids)
        return [
            self._joined(self.records[i])
            for i in reversed(self._order)
            if i in wanted and self.records[i].owner == owner
        ]

    async def delete(self, upload_id: str, owner: str | None = None) -> UploadRecord | None:
        record = self.records.get(upload_id)
        if record is None or (owner is not None and record.owner != owner):
            return None
        del self.records[upload_id]
        self._order.remove(upload_id)
        return record

    async def find_ready_by_hash(self, owner: str, content_hash: str) -> UploadRecord | None:
        for i in reversed(self._order):
            record = self.records[i]
            if (
                record.owner == owner
                and record.content_hash == content_hash
                and record.status == STATUS_READY
            ):
                return self._joined(record)
        return None

    async def find_active_video_by_hash(self, owner: str, content_hash: str) -> UploadRecord | None:
        for i in reversed(self._order):
            record = self.records[i]
            if (
                record.owner == owner
                and record.content_hash == content_hash
                and record.kind == KIND_VIDEO
                and record.status in (STATUS_READY, STATUS_PROCESSING)
            ):
                return self._joined(record)
        return None

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
        record = self.records.get(upload_id)
        if record is None:
            return None
        updated = replace(
            record,
            storage_key=storage_key,
            size_bytes=size_bytes,
            duration_seconds=duration_seconds,
            truncated=truncated,
            width=width if width is not None else record.width,
            height=height if height is not None else record.height,
            content_type=content_type if content_type is not None else record.content_type,
            status=STATUS_READY,
            updated_at=datetime.now(UTC),
        )
        self.records[upload_id] = updated
        return updated

    async def mark_failed(self, upload_id: str) -> UploadRecord | None:
        record = self.records.get(upload_id)
        if record is None:
            return None
        updated = replace(record, status=STATUS_FAILED, updated_at=datetime.now(UTC))
        self.records[upload_id] = updated
        return updated

    async def get_by_id(self, upload_id: str) -> UploadRecord | None:
        record = self.records.get(upload_id)
        return self._joined(record) if record is not None else None

    async def set_poster(self, video_id: str, poster_upload_id: str) -> UploadRecord | None:
        record = self.records.get(video_id)
        if record is None:
            return None
        # poster_storage_key is never persisted here (real Postgres has no
        # such column either) -- only poster_upload_id is stored, and
        # `_joined` resolves the key fresh on every read, same as the real
        # store's LEFT JOIN. Mirrors PostgresMetadataStore.set_poster, which
        # updates the FK column then re-fetches via the joined get_by_id.
        updated = replace(record, poster_upload_id=poster_upload_id, updated_at=datetime.now(UTC))
        self.records[video_id] = updated
        return self._joined(updated)

    async def mark_webhook(
        self,
        upload_id: str,
        *,
        status: str,
        attempts: int = 0,
        last_error: str | None = None,
    ) -> UploadRecord | None:
        record = self.records.get(upload_id)
        if record is None:
            return None
        updated = replace(
            record,
            webhook_status=status,
            webhook_attempts=attempts,
            webhook_last_error=last_error,
            webhook_updated_at=datetime.now(UTC),
        )
        self.records[upload_id] = updated
        return updated

    async def set_visibility(
        self,
        upload_id: str,
        owner: str,
        visibility: str,
        storage_key: str | None = None,
        renditions: dict[str, str] | None = None,
    ) -> UploadRecord | None:
        record = self.records.get(upload_id)
        if record is None or record.owner != owner:
            return None
        updated = replace(
            record,
            visibility=visibility,
            # `is not None`, not `or` -- matches the real SQL's
            # `storage_key = COALESCE($4, storage_key)`, which only treats a
            # NULL parameter as "keep the old value" (an explicit empty
            # string would overwrite it). `renditions` right below already
            # gets this right; `storage_key` didn't.
            storage_key=storage_key if storage_key is not None else record.storage_key,
            renditions=renditions if renditions is not None else record.renditions,
            updated_at=datetime.now(UTC),
        )
        self.records[upload_id] = updated
        return updated

    async def set_share_token(self, upload_id: str, owner: str, token: str) -> UploadRecord | None:
        record = self.records.get(upload_id)
        if record is None or record.owner != owner:
            return None
        updated = replace(record, share_token=token, updated_at=datetime.now(UTC))
        self.records[upload_id] = updated
        return updated

    async def clear_share_token(self, upload_id: str, owner: str) -> UploadRecord | None:
        record = self.records.get(upload_id)
        if record is None or record.owner != owner:
            return None
        updated = replace(record, share_token=None, updated_at=datetime.now(UTC))
        self.records[upload_id] = updated
        return updated

    async def get_by_share_token(self, token: str) -> UploadRecord | None:
        for i in reversed(self._order):
            record = self.records[i]
            if record.share_token == token:
                return self._joined(record)
        return None

    async def aclose(self) -> None:
        self.closed = True
