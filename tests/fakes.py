"""Test doubles for app.services.storage.StorageBackend and metadata.MetadataStore."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from app.services.metadata import (
    KIND_VIDEO,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_READY,
    MetadataStore,
    UploadRecord,
)
from app.services.storage import StorageBackend, StorageError, StorageObject


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
            raise StorageError(f"Object not found: {key!r}") from exc

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)
        self.deleted_keys.append(key)

    def public_url(self, key: str) -> str:
        return f"{self._base_url}/{key}"

    async def presigned_get_url(self, key: str, expires_in: int = 3600) -> str | None:
        if not self._presign_capable:
            return None
        return f"{self._base_url}/{key}?X-Amz-Signature=fake&expires={expires_in}"

    async def aclose(self) -> None:
        self.closed = True


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
            created_at=now,
            updated_at=now,
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
        return record if record is not None and record.owner == owner else None

    async def get_by_task_id(self, task_id: str, owner: str) -> UploadRecord | None:
        for i in reversed(self._order):
            record = self.records[i]
            if record.task_id == task_id and record.owner == owner:
                return record
        return None

    async def list(
        self, owner: str, *, kind: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[UploadRecord]:
        matches = [
            self.records[i]
            for i in reversed(self._order)
            if self.records[i].owner == owner and (kind is None or self.records[i].kind == kind)
        ]
        return matches[offset : offset + limit]

    async def delete(self, upload_id: str, owner: str) -> UploadRecord | None:
        record = self.records.get(upload_id)
        if record is None or record.owner != owner:
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
                return record
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
                return record
        return None

    async def mark_ready(
        self,
        upload_id: str,
        *,
        storage_key: str,
        size_bytes: int,
        duration_seconds: float | None = None,
        truncated: bool = False,
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

    async def aclose(self) -> None:
        self.closed = True
