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

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Image upload
# ---------------------------------------------------------------------------


_RECORD_ID_DESC = "The upload record id (use with /files/{id})"
_STORAGE_KEY_DESC = "Storage key of the uploaded image (public ready images only)"
_RENDITIONS_DESC = "Storage keys for materialized renditions (public ready images only)"
_ORIGINAL_FILENAME_DESC = "Original upload filename if provided"
_SIZE_BYTES_DESC = "Stored (WebP) object size in bytes"
_SIZE_MB_DESC = "Stored size in megabytes, rounded to 2dp (null for a 0-byte object)"
_URL_DESC = (
    "The URL to view/download the full-size image: direct CDN URL on object "
    "storage with a public base URL configured, or GET /files/{id}/download on local/private."
)
_THUMBNAIL_URL_DESC = (
    "Same value and resolution as GET /files/{id}'s thumbnail_url: the "
    "materialized 300x300 fill thumbnail's direct object/CDN URL, or a signed imgproxy "
    "URL (with extension) when no public base URL is configured. Present when thumbnail "
    "is requested on public uploads."
)
_CUSTOM_URL_DESC = (
    "Signed imgproxy URL (with extension) for the requested custom "
    "width/height/fit/format; present only when custom transform parameters were "
    "supplied on a public upload"
)
_DOMINANT_COLOR_DESC = (
    "Average dominant colour of the image formatted as a 7-character hex string (e.g. #1e293b)"
)
_BLUR_DATA_URL_DESC = (
    "16px WebP encoded as a data:image/webp;base64,... URI for blurred image placeholder preview"
)


class ImageDimensions(BaseModel):
    """Pixel dimensions of a stored image (either may be null if unknown)."""

    width: int | None = Field(default=None, description="Pixel width")
    height: int | None = Field(default=None, description="Pixel height")


class _BaseImageUploadResponse(BaseModel):
    """Shared fields for single and bulk successful image upload responses."""

    id: str = Field(description=_RECORD_ID_DESC)
    storage_key: str | None = Field(default=None, description=_STORAGE_KEY_DESC)
    renditions: dict[str, str] | None = Field(default=None, description=_RENDITIONS_DESC)
    original_filename: str | None = Field(default=None, description=_ORIGINAL_FILENAME_DESC)
    size_bytes: int | None = Field(default=None, description=_SIZE_BYTES_DESC)
    size_mb: float | None = Field(default=None, description=_SIZE_MB_DESC)
    dimensions: ImageDimensions
    url: str | None = Field(default=None, description=_URL_DESC)
    thumbnail_url: str | None = Field(default=None, description=_THUMBNAIL_URL_DESC)
    custom_url: str | None = Field(default=None, description=_CUSTOM_URL_DESC)
    dominant_color: str | None = Field(default=None, description=_DOMINANT_COLOR_DESC)
    blur_data_url: str | None = Field(default=None, description=_BLUR_DATA_URL_DESC)


class ImageUploadResponse(_BaseImageUploadResponse):
    """Returned by ``POST /upload/image``."""

    status: str = Field(description="Always 'success' for a completed image upload")


class BulkImageUploadItemSuccess(_BaseImageUploadResponse):
    """A successfully uploaded image in a bulk upload batch."""

    status: Literal["success"] = Field(
        default="success", description="Status indicator: 'success' for successfully uploaded image"
    )


class BulkImageUploadItemError(BaseModel):
    """A failed image item in a bulk upload batch."""

    status: Literal["error"] = Field(
        default="error", description="Status indicator: 'error' for failed image"
    )
    code: Literal["too_large", "batch_too_large", "invalid_image", "processing_failed"] = Field(
        description="Machine-readable error code"
    )
    message: str = Field(description="Human-readable error explanation for debugging")
    original_filename: str | None = Field(default=None, description=_ORIGINAL_FILENAME_DESC)


BulkImageUploadItem = Annotated[
    BulkImageUploadItemSuccess | BulkImageUploadItemError, Field(discriminator="status")
]


class BulkImageUploadResponse(BaseModel):
    """Returned by ``POST /upload/images``."""

    succeeded: int = Field(description="Number of successfully uploaded images in the batch")
    failed: int = Field(description="Number of failed images in the batch")
    total: int = Field(description="Total number of files evaluated in the batch")
    items: list[BulkImageUploadItem] = Field(
        description="Per-file results in exact 1-to-1 correspondence with the uploaded files array"
    )


# ---------------------------------------------------------------------------
# Video upload (two shapes, discriminated on `status`)
# ---------------------------------------------------------------------------


class VideoUploadAcceptedResponse(BaseModel):
    """``POST /upload/video`` when the upload was newly accepted for compression."""

    status: Literal["accepted"] = Field(description="Upload accepted; compression enqueued")
    id: str = Field(description="The upload record id (poll /files/{id} until status='ready')")
    task_id: str = Field(description="Compression task id (poll /tasks/{task_id})")


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
# Generic file upload
# ---------------------------------------------------------------------------


class FileUploadResponse(BaseModel):
    """Returned by ``POST /upload/file``."""

    status: str = Field(description="Always 'success' for a completed file upload")
    id: str = Field(description=_RECORD_ID_DESC)
    kind: str = Field(description="Always 'file' for generic file uploads")
    content_type: str = Field(description="MIME type of the uploaded file")
    size_bytes: int | None = Field(default=None, description="Stored object size in bytes")
    size_mb: float | None = Field(
        default=None,
        description=_SIZE_MB_DESC,
    )
    original_filename: str | None = Field(default=None, description="Original upload filename")
    visibility: str = Field(description="'private' | 'public'")
    url: str | None = Field(
        default=None,
        description="The canonical download URL: GET /files/{id}/download",
    )


# ---------------------------------------------------------------------------
# File records (mirror UploadRecord.to_public())
# ---------------------------------------------------------------------------


class FileRecord(BaseModel):
    """One upload record, exactly as ``UploadRecord.to_public()`` serializes it.

    ``url`` / ``poster_url`` / ``storage_key`` / ``renditions`` are present only for a
    ``public`` and ``ready`` record (rendered via ``response_model_exclude_unset``);
    the secret ``share_token`` is never here.
    """

    id: str
    kind: str = Field(description="'image' | 'video' | 'file'")
    status: str = Field(description="'processing' | 'ready' | 'failed'")
    storage_key: str | None = Field(
        default=None, description="Object storage key for the file (public ready records only)"
    )
    renditions: dict[str, str] | None = Field(
        default=None,
        description=_RENDITIONS_DESC,
    )
    content_type: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
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
        default=None,
        description=(
            "The URL to view/download the file: direct CDN URL on object storage with a "
            "public base URL configured, or canonical GET /files/{id}/download on local "
            "storage or private records. Present once status='ready'."
        ),
    )
    thumbnail_url: str | None = Field(
        default=None,
        description=(
            "Public URL for a 300x300 fill thumbnail. On object stores with a public base URL "
            "configured, this is a direct CDN/object read of the materialized rendition; "
            "otherwise signed imgproxy URL (with extension). Public images only."
        ),
    )
    poster_url: str | None = Field(
        default=None,
        description=(
            "Public URL for the video's poster image: direct CDN URL when a public base URL "
            "is configured, otherwise signed imgproxy URL. Public records only."
        ),
    )
    dominant_color: str | None = Field(default=None, description=_DOMINANT_COLOR_DESC)
    blur_data_url: str | None = Field(default=None, description=_BLUR_DATA_URL_DESC)
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


class FileBatchRequest(BaseModel):
    """Request body for ``POST /files/batch``."""

    ids: list[str] = Field(
        min_length=1,
        max_length=200,
        description="List of upload record ids. Up to 200 per request.",
    )


class FileBatchResponse(BaseModel):
    """Returned by ``POST /files/batch``.

    One round trip for many records instead of one ``GET /files/{id}`` per id
    -- the shape a listing page (many photos across many parent records)
    actually needs. An id that doesn't exist or belongs to another owner is
    silently omitted, not an error (existence never leaks, same as every
    other owner-scoped route); order is not guaranteed to match the
    requested ids.
    """

    files: list[FileRecord]


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


class WhoAmIResponse(BaseModel):
    """Returned by ``GET /whoami``."""

    owner: str = Field(description="The authenticated tenant/owner identity")


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
