"""Pydantic response models for every JSON-returning API endpoint.

These exist for two reasons: OpenAPI/Swagger documentation (so a consumer can
see the exact shape of each response without reading the source) and runtime
response validation (FastAPI validates the returned dict against the model).

They are **descriptive, not prescriptive**: each model was written to match the
dict a route already returns, not the other way around. Where a route returns
one of several shapes (a video upload can be `accepted` or `duplicate`; a task
can be `pending`/`completed`/`failed`) the model is a union keyed on a `Literal`
status, and the route is wired with ``response_model_exclude_unset=True`` so a
field that wasn't in the returned dict is not rendered as ``null`` -- the
serialized body stays byte-for-byte what it was before the model was added.

The record-shaped fields (``FileRecord``) mirror ``UploadRecord.to_public()``
exactly, including that the datetime columns arrive as pre-formatted ISO-8601
*strings* (``to_public`` already calls ``.isoformat()``), so they are typed as
``str`` here to pass through untouched. ``share_token`` is deliberately absent --
it is a secret capability returned only by the share-mint endpoint.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Image upload
# ---------------------------------------------------------------------------


class ImageDimensions(BaseModel):
    """Pixel dimensions of a stored image (either may be null if unknown)."""

    width: int | None = Field(default=None, description="Pixel width")
    height: int | None = Field(default=None, description="Pixel height")


class ImageUploadResponse(BaseModel):
    """Returned by ``POST /upload/image``."""

    status: str = Field(description="Always 'success' for a completed image upload")
    id: str = Field(description="The upload record id (use with /files/{id})")
    size_bytes: int | None = Field(default=None, description="Stored (WebP) object size in bytes")
    size_mb: float | None = Field(
        default=None,
        description="Stored size in megabytes, rounded to 2dp (null for a 0-byte object)",
    )
    dimensions: ImageDimensions
    imgproxy_thumbnail_url: str = Field(description="Signed imgproxy URL: a 300x300 fill thumbnail")
    imgproxy_optimized_url: str = Field(
        description="Signed imgproxy URL: the image auto-optimized at full size"
    )
    imgproxy_custom_url: str | None = Field(
        default=None,
        description="Signed imgproxy URL for the requested custom width/height/fit/format; "
        "present only when custom transform parameters were supplied",
    )


class BulkImageUploadResponse(BaseModel):
    """Returned by ``POST /upload/images``."""

    count: int = Field(description="Number of successfully uploaded images")
    items: list[ImageUploadResponse]


# ---------------------------------------------------------------------------
# Video upload (two shapes, discriminated on `status`)
# ---------------------------------------------------------------------------


class VideoUploadAcceptedResponse(BaseModel):
    """``POST /upload/video`` when the upload was newly accepted for compression."""

    status: Literal["accepted"] = Field(description="Upload accepted; compression enqueued")
    id: str = Field(description="The upload record id (poll /files/{id} until status='ready')")
    task_id: str = Field(description="Compression task id (poll /tasks/{task_id})")
    raw_key: str = Field(description="Storage key of the staged raw upload")


class VideoUploadDuplicateResponse(BaseModel):
    """``POST /upload/video`` when identical bytes were already uploaded by this owner."""

    status: Literal["duplicate"] = Field(
        description="Identical raw bytes already uploaded by this owner; the existing job is reused"
    )
    id: str = Field(description="The existing upload record id")
    task_id: str | None = Field(
        default=None, description="Compression task id of the existing job (may be null)"
    )
    record_status: str = Field(
        description="Status of the existing record: 'ready' (available) or 'processing' (in flight)"
    )


VideoUploadResponse = VideoUploadAcceptedResponse | VideoUploadDuplicateResponse


# ---------------------------------------------------------------------------
# File records (mirror UploadRecord.to_public())
# ---------------------------------------------------------------------------


class FileRecord(BaseModel):
    """One upload record, exactly as ``UploadRecord.to_public()`` serializes it.

    ``url`` / ``poster_url`` are present only for a ``ready`` record (rendered via
    ``response_model_exclude_unset``); the secret ``share_token`` is never here.
    """

    id: str
    kind: str = Field(description="'image' or 'video'")
    status: str = Field(description="'processing' | 'ready' | 'failed'")
    storage_key: str
    content_type: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    content_hash: str | None = None
    task_id: str | None = None
    original_filename: str | None = None
    duration_seconds: float | None = None
    truncated: bool = Field(
        description="True if a long video's output was capped to the max duration"
    )
    callback_url: str | None = None
    poster_upload_id: str | None = None
    webhook_status: str | None = Field(
        default=None, description="'pending' | 'delivered' | 'failed', or null if no callback"
    )
    webhook_attempts: int = 0
    webhook_last_error: str | None = None
    webhook_updated_at: str | None = Field(default=None, description="ISO-8601 timestamp, or null")
    visibility: str = Field(description="'private' | 'public'")
    url: str | None = Field(
        default=None, description="Stable playback/image URL; present only when status='ready'"
    )
    thumbnail_url: str | None = Field(
        default=None, description="Signed imgproxy URL: a 300x300 fill thumbnail"
    )
    poster_url: str | None = Field(
        default=None, description="Direct signed imgproxy URL for the video poster image"
    )
    created_at: str = Field(description="ISO-8601 timestamp")
    updated_at: str = Field(description="ISO-8601 timestamp")


class FileListResponse(BaseModel):
    """Returned by ``GET /files``."""

    files: list[FileRecord]
    total_count: int = Field(
        description="Total records matching the owner (and `kind` filter); ignores limit/offset"
    )
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class TaskStatusResponse(BaseModel):
    """Returned by ``GET /tasks/{task_id}``.

    Note the actual route returns ``task_id`` + ``status`` and then *either*
    ``result`` (completed) *or* ``error`` (failed); ``pending`` carries neither.
    Wired with ``response_model_exclude_unset`` so only the keys the route
    actually set are rendered.
    """

    task_id: str
    status: str = Field(description="'pending' | 'completed' | 'failed'")
    result: dict[str, Any] | None = Field(
        default=None, description="Execution result of a completed task (thin execution state)"
    )
    error: str | None = Field(default=None, description="Sanitized error message for a failed task")


# ---------------------------------------------------------------------------
# Sharing & playback
# ---------------------------------------------------------------------------


class ShareLinkResponse(BaseModel):
    """Returned by ``POST /files/{id}/share`` -- the ONLY place the token appears."""

    id: str
    share_token: str = Field(
        description="Secret capability token. Returned here only, never in listings or webhooks"
    )
    share_url: str = Field(description="Shareable URL embedding the token")


# ---------------------------------------------------------------------------
# Posters (two shapes, discriminated on `status`)
# ---------------------------------------------------------------------------


class PosterReadyResponse(BaseModel):
    """``POST /files/{id}/poster`` when a poster already exists (returned inline)."""

    status: Literal["ready"]
    video_id: str
    poster: FileRecord


class PosterAcceptedResponse(BaseModel):
    """``POST /files/{id}/poster`` when generation was enqueued."""

    status: Literal["accepted"]
    video_id: str
    task_id: str
    poll: str = Field(
        description="URL to poll (GET /files/{video_id}) until poster_upload_id is set"
    )


PosterResponse = PosterReadyResponse | PosterAcceptedResponse


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


class WebhookRedeliverResponse(BaseModel):
    """Returned by ``POST /files/{id}/redeliver``."""

    status: Literal["accepted"]
    id: str
    event: str = Field(description="Terminal event re-enqueued: 'video.completed' | 'video.failed'")
    task_id: str


# ---------------------------------------------------------------------------
# System / health
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """Returned by ``GET /healthz``."""

    status: str = Field(description="Always 'ok' when the process can serve requests")


class ReadinessChecks(BaseModel):
    """Per-dependency readiness, each 'ok' or 'unavailable'."""

    redis: str
    storage: str
    db: str


class ReadinessResponse(BaseModel):
    """Returned by ``GET /readyz`` (200 when ready, 503 otherwise)."""

    status: str = Field(description="'ok' when every dependency is reachable, else 'unavailable'")
    checks: ReadinessChecks
