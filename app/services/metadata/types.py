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
KIND_FILE = "file"

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
    """Thin seam over the shared imgproxy helper, kept so this module's imports
    stay lazy (see the module docstring: ``types`` is dependency-free at import
    time so ``store`` and ``postgres`` can build on it without a cycle)."""
    from app.services.imgproxy import signed_image_url

    return signed_image_url(storage_key, processing_options=processing_options)


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
    renditions: dict[str, str] | None = None

    def _populate_thumbnail_url(self, data: dict[str, Any]) -> None:
        """Static thumbnail URL for a public, ready image record."""
        if self.kind != KIND_IMAGE:
            return

        from app.services.renditions import derive_thumbnail_url

        if self.renditions and "thumbnail" in self.renditions:
            data["thumbnail_url"] = derive_thumbnail_url(self.storage_key, self.renditions)
        else:
            data["thumbnail_url"] = derive_thumbnail_url(self.storage_key, None)

    def _populate_poster_url(self, data: dict[str, Any], poster_storage_key: str | None) -> None:
        """Static poster URL for a public, ready video record."""
        if not self.poster_upload_id:
            return

        pkey = poster_storage_key or self.poster_storage_key
        if not pkey:
            return

        from app.services.storage import has_public_base_url, public_object_url

        if has_public_base_url():
            data["poster_url"] = public_object_url(pkey)
        else:
            data["poster_url"] = _build_imgproxy_url(pkey)

    def _populate_ready_urls(self, data: dict[str, Any], poster_storage_key: str | None) -> None:
        """Helper to populate playback URLs for a ready record."""
        from app.services.storage import has_public_base_url, public_object_url
        from app.urls import public_url

        if self.visibility == VISIBILITY_PUBLIC and has_public_base_url():
            data["url"] = public_object_url(self.storage_key)
        else:
            data["url"] = public_url(f"/files/{self.id}/download")

        if self.visibility != VISIBILITY_PUBLIC:
            return

        self._populate_thumbnail_url(data)
        self._populate_poster_url(data, poster_storage_key)

    def _filtered_renditions(self) -> dict[str, str]:
        """Return renditions mapping, excluding width specs exceeding source width."""
        if not self.renditions:
            return {}
        from app.services.renditions import get_rendition_spec

        result: dict[str, str] = {}
        for name, key in self.renditions.items():
            spec = get_rendition_spec(name)
            if spec and not spec.crop and self.width is not None and spec.width > self.width:
                continue
            result[name] = key
        return result

    def to_public(self, poster_storage_key: str | None = None) -> dict[str, Any]:
        """Owner-safe JSON view for API responses (no cross-tenant fields)."""
        data = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
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
            "visibility": self.visibility,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

        if self.status == STATUS_READY:
            self._populate_ready_urls(data, poster_storage_key)
            if self.visibility == VISIBILITY_PUBLIC:
                data["storage_key"] = self.storage_key
                data["renditions"] = self._filtered_renditions()

        return data
