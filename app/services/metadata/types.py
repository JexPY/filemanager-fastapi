"""The ``UploadRecord`` dataclass and the string constants that describe a row.

Kept dependency-free (imports nothing from the rest of the metadata package) so
both the store interface and the Postgres backend can build on it without a
cycle. The one outward import is the top-level ``app.urls`` helper, resolved
lazily inside ``to_public`` (see the inverted-dependency note there).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Allowed record states, kept here so callers don't sprinkle string literals.
STATUS_READY = "ready"
STATUS_PROCESSING = "processing"
STATUS_FAILED = "failed"

KIND_IMAGE = "image"
KIND_VIDEO = "video"

# Playback visibility of a record. `private` is owner-only (authenticated
# /files/{id}/download); `public` is anyone-with-the-link (no token).
VISIBILITY_PRIVATE = "private"
VISIBILITY_PUBLIC = "public"

# Webhook delivery states persisted on a row (for the dead-letter / redelivery
# path). NULL means no callback_url or never attempted.
WEBHOOK_PENDING = "pending"
WEBHOOK_DELIVERED = "delivered"
WEBHOOK_FAILED = "failed"


def _build_imgproxy_url(storage_key: str, processing_options: str = "rs:auto") -> str:
    from app.config import settings
    from app.services.imgproxy import build_source_url, generate_signed_url

    if settings.STORAGE_BACKEND == "local":
        source_url = build_source_url(storage_key, "")
    else:
        from app.services.storage import _build_backend, _storage

        backend = _storage if _storage is not None else _build_backend()
        source_url = build_source_url(storage_key, backend.public_url(storage_key))

    return generate_signed_url(source_url, processing_options=processing_options)


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
    callback_url: str | None
    poster_upload_id: str | None
    webhook_status: str | None
    webhook_attempts: int
    webhook_last_error: str | None
    webhook_updated_at: datetime | None
    visibility: str
    share_token: str | None
    created_at: datetime
    updated_at: datetime
    poster_storage_key: str | None = None

    def to_public(self, poster_storage_key: str | None = None) -> dict[str, Any]:
        """Owner-safe JSON view for API responses (no cross-tenant fields)."""
        data = {
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
            "callback_url": self.callback_url,
            "poster_upload_id": self.poster_upload_id,
            "webhook_status": self.webhook_status,
            "webhook_attempts": self.webhook_attempts,
            "webhook_last_error": self.webhook_last_error,
            "webhook_updated_at": (
                self.webhook_updated_at.isoformat() if self.webhook_updated_at else None
            ),
            # `visibility` is safe to expose; `share_token` is a secret capability
            # and is deliberately omitted here -- it's returned only by the
            # share-mint endpoint, never in listings/webhooks/GET /files/{id}.
            "visibility": self.visibility,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

        if self.status == STATUS_READY:
            from app.urls import public_url

            if self.kind == KIND_VIDEO:
                data["url"] = public_url(f"/files/{self.id}/download")
            elif self.kind == KIND_IMAGE:
                data["url"] = _build_imgproxy_url(self.storage_key)
                data["thumbnail_url"] = _build_imgproxy_url(
                    self.storage_key, processing_options="rs:fill:300:300:0/g:no"
                )

            if self.poster_upload_id:
                pkey = poster_storage_key or self.poster_storage_key or self.poster_upload_id
                data["poster_url"] = _build_imgproxy_url(pkey)

        return data
