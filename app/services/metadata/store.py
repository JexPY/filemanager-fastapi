"""The backend-agnostic store interface and its error type.

``MetadataStore`` is the abstract contract both the real Postgres backend and
the in-memory test fake implement; ``MetadataError`` is the sanitized failure
type that never carries DB internals to callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from .types import UploadRecord


class MetadataError(Exception):
    """Backend-agnostic metadata-store failure. Never carries DB internals to callers."""


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
        visibility: str = "private",
        **kwargs: object,
    ) -> UploadRecord:
        """Create a new upload record. Optional metadata fields such as
        ``callback_url``, ``renditions``, ``dominant_color``, and ``blur_data_url``
        are accepted via ``**kwargs`` to stay within parameter count limits."""
        ...

    @abstractmethod
    async def set_task_id(self, upload_id: str, task_id: str) -> None:
        """Attach the compression task id to a just-created video row. Called
        after enqueue (the id only exists then); a missing row is a no-op."""

    @abstractmethod
    async def get(self, upload_id: str, owner: str) -> UploadRecord | None: ...

    @abstractmethod
    async def get_by_task_id(self, task_id: str, owner: str) -> UploadRecord | None:
        """Owner-scoped lookup by compression task id, so `GET /tasks/{id}` can
        reject another owner's task with a 404 (existence never leaks). The row
        is also the durable proof the task was ever issued."""

    @abstractmethod
    async def list(
        self,
        owner: str,
        *,
        kind: str | None = None,
        status: str | None = None,
        visibility: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[UploadRecord]: ...

    @abstractmethod
    async def count(
        self,
        owner: str,
        *,
        kind: str | None = None,
        status: str | None = None,
        visibility: str | None = None,
    ) -> int:
        """Total records for this owner (optionally filtered by `kind`, `status`,
        `visibility`), ignoring limit/offset -- the `total_count` a paginated
        `GET /files` reports so a client can size its pager without walking every page."""

    @abstractmethod
    async def get_many(self, owner: str, upload_ids: Sequence[str]) -> Sequence[UploadRecord]:
        """Owner-scoped batch lookup by id, for a caller (e.g. a listing page
        rendering many records at once) that would otherwise need one
        `GET /files/{id}` round trip per id. An id that doesn't exist or
        belongs to another owner is silently omitted from the result rather
        than erroring -- existence never leaks, same as `get`. Order is not
        guaranteed to match `upload_ids`; callers that need a stable order
        should re-sort by id themselves.

        Both the parameter and return types are spelled `Sequence[...]`, not
        `list[...]` -- this class already defines a `list` method, and
        mypy/pyright resolve a bare `list[...]` annotation on a later member
        against that name instead of the builtin (a real, reproducible
        shadowing quirk, not a style choice)."""

    @abstractmethod
    async def delete(self, upload_id: str, owner: str | None = None) -> UploadRecord | None:
        """Delete the row (owner-scoped if owner provided, else unscoped) and return it."""

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
        width: int | None = None,
        height: int | None = None,
        content_type: str | None = None,
    ) -> UploadRecord | None:
        """Flip a processing row to ready. None means the row is gone (deleted
        mid-flight) -- the caller owns cleaning up whatever it just wrote.
        `duration_seconds`/`truncated` are the worker's ffprobe findings for a
        video (the input's duration and whether the output was capped)."""

    @abstractmethod
    async def mark_failed(self, upload_id: str) -> UploadRecord | None: ...

    @abstractmethod
    async def get_by_id(self, upload_id: str) -> UploadRecord | None:
        """Unscoped lookup by id, for worker-internal tasks (poster/webhook
        delivery) that already ran through an owner-scoped admission in the api.
        Route handlers must use the owner-scoped `get` instead."""

    @abstractmethod
    async def set_poster(self, video_id: str, poster_upload_id: str) -> UploadRecord | None:
        """Point a video row at its poster image record. None means the video row
        is gone (owner DELETEd it mid-generation) -- the caller owns cleaning up
        the poster it just wrote, mirroring mark_ready's race handling."""

    @abstractmethod
    async def mark_webhook(
        self,
        upload_id: str,
        *,
        status: str,
        attempts: int = 0,
        last_error: str | None = None,
    ) -> UploadRecord | None:
        """Persist the outcome of a webhook delivery cycle on the row (the
        dead-letter record). `status` is one of WEBHOOK_PENDING/DELIVERED/FAILED."""

    @abstractmethod
    async def set_visibility(
        self,
        upload_id: str,
        owner: str,
        visibility: str,
        storage_key: str | None = None,
        renditions: dict[str, str] | None = None,
    ) -> UploadRecord | None:
        """Owner-scoped visibility change (VISIBILITY_PRIVATE/PUBLIC). None means
        no such row for this owner (unknown id or another owner's -> 404 upstream).

        ``storage_key`` re-points the row at a rotated object in the same
        statement -- turning a record private copies it to a fresh key so that
        URLs already cached in a CDN stop resolving."""

    @abstractmethod
    async def set_share_token(self, upload_id: str, owner: str, token: str) -> UploadRecord | None:
        """Owner-scoped mint/rotate of the share capability token. None means no
        such row for this owner."""

    @abstractmethod
    async def clear_share_token(self, upload_id: str, owner: str) -> UploadRecord | None:
        """Owner-scoped revoke of the share token (sets it NULL). None means no
        such row for this owner."""

    @abstractmethod
    async def get_by_share_token(self, token: str) -> UploadRecord | None:
        """Unscoped lookup by share token -- the token *is* the grant (like
        get_by_id for the worker), so no owner scoping. Unknown token -> None."""

    async def aclose(self) -> None:  # noqa: B027
        """Release any pooled clients. Default is a no-op."""
