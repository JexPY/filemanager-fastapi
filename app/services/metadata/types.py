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
            # `visibility` is safe to expose; `share_token` is a secret capability
            # and is deliberately omitted here -- it's returned only by the
            # share-mint endpoint, never in listings/webhooks/GET /files/{id}.
            "visibility": self.visibility,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

        if self.status == STATUS_READY:
            from app.services.storage import has_public_base_url, public_object_url
            from app.urls import public_url

            # ONE canonical URL, same for every kind. It is permanent and
            # backend-agnostic: switching STORAGE_BACKEND, moving behind a CDN,
            # or flipping visibility all leave it untouched, and it is the only
            # URL here that resolves for a private record. Consumers should
            # persist the record id and this URL, nothing else.
            data["url"] = public_url(f"/files/{self.id}/download")

            # Everything below is an *accelerator*, not the address of record:
            # a direct, no-redirect URL for LCP-sensitive embedding, and imgproxy
            # renditions. All of it is public-only, because both forms are
            # unexpiring bearer URLs with no ownership check -- a private record
            # is reachable solely through the app route above.
            if self.visibility == VISIBILITY_PUBLIC:
                if has_public_base_url():
                    data["direct_url"] = public_object_url(self.storage_key)

                if self.kind == KIND_IMAGE:
                    from app.services.renditions import derive_medium_url, derive_thumbnail_url

                    data["thumbnail_url"] = derive_thumbnail_url(self.storage_key, self.renditions)
                    data["medium_url"] = derive_medium_url(self.storage_key, self.renditions)

                if self.poster_upload_id:
                    # `pkey` is a *storage key*, never the bare poster record
                    # id -- falling back to `self.poster_upload_id` here used
                    # to build a URL that treated a record id as if it were an
                    # object key, producing a dead/404 poster_url instead of
                    # simply omitting it. If neither source has the real key
                    # (the LEFT JOIN didn't resolve it), omit both fields.
                    pkey = poster_storage_key or self.poster_storage_key
                    if pkey:
                        data["poster_url"] = _build_imgproxy_url(pkey)
                        # A static companion to poster_url: posters are their
                        # own image record, so their plain object URL is a
                        # direct CDN/bucket read -- no imgproxy involved, no
                        # live encode, unlike poster_url above (always a live
                        # imgproxy fetch).
                        if has_public_base_url():
                            data["poster_direct_url"] = public_object_url(pkey)

        return data
