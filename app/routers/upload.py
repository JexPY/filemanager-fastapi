import asyncio
import contextlib
import hashlib
import logging
import os
import uuid
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)

from app.config import settings
from app.routers.auth import require_scopes
from app.routers.utils import (
    _image_response,
    _image_source_url,
    _read_capped,
    _sanitize_extension,
    _sha256_hex,
    _stream_capped_to_temp,
)
from app.schemas import BulkImageUploadResponse, ImageUploadResponse, VideoUploadResponse
from app.services.image_vips import ImageValidationError, validate_and_strip_image
from app.services.metadata import (
    KIND_IMAGE,
    KIND_VIDEO,
    STATUS_PROCESSING,
    STATUS_READY,
    MetadataError,
    get_metadata_store,
)
from app.services.storage import StorageError, delete_file, upload_file, upload_file_from_path
from app.services.webhooks import WebhookValidationError, validate_callback_url
from app.tasks import compress_video_task

logger = logging.getLogger(__name__)
router = APIRouter()

# Scope-gated auth: a static master token passes unconditionally (full access);
# a capability JWT must carry the matching upload scope, else 403. Built once at
# import so each route reuses one stable dependency callable.
require_image_upload = require_scopes("upload:image")
require_video_upload = require_scopes("upload:video")


async def _process_single_image(
    file_data: bytes,
    owner: str,
    optimization: str,
    imgproxy_width: int | None,
    imgproxy_height: int | None,
    imgproxy_fit: str,
    imgproxy_format: str | None,
    *,
    raise_on_error: bool,
) -> dict | None:
    """Process one image upload end-to-end: sha256 hash -> dedup check ->
    validate/strip -> store -> record. If raise_on_error=True (single upload
    endpoint), propagates HTTPException on client/server failures. If False
    (bulk endpoint), returns None on any failure so the batch continues. Never
    leaks storage internals to the caller."""
    try:
        # Content hash of the *input* bytes for idempotency. Hashing 25 MB is
        # borderline CPU work, so offload it like the other CPU-bound steps.
        # We include the optimization profile in the hash, so different
        # optimization levels are treated as distinct uploads for the same
        # source bytes.
        signature = f"{await asyncio.to_thread(_sha256_hex, file_data)}:{optimization}"
        content_hash = hashlib.sha256(signature.encode()).hexdigest()

        store = await get_metadata_store()
        # Idempotency: the exact same bytes already stored (and ready) by this
        # owner returns the existing record instead of re-decoding/
        # re-encoding/re-storing. Owner-scoped so hashes never collide or leak
        # across tenants. A lookup failure must not fail the upload -- fall
        # through and process normally.
        try:
            existing = await store.find_ready_by_hash(owner, content_hash)
        except MetadataError as exc:
            logger.warning("Idempotency lookup failed (processing normally): %s", exc)
            existing = None
        if existing is not None:
            source_url = await _image_source_url(existing.storage_key)
            return _image_response(
                existing.id,
                existing.width,
                existing.height,
                existing.size_bytes,
                source_url,
                custom_width=imgproxy_width,
                custom_height=imgproxy_height,
                custom_fit=imgproxy_fit,
                custom_format=imgproxy_format,
            )

        # Client-side failures (bad/unsupported image) => 400, generic detail.
        try:
            optimized_buffer, content_type, width, height = await asyncio.to_thread(
                validate_and_strip_image, file_data, optimization
            )
        except ImageValidationError as exc:
            logger.warning("Image validation rejected upload: %s", exc)
            if not raise_on_error:
                return None
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or unsupported image"
            ) from exc

        unique_id = uuid.uuid4().hex
        object_name = f"images/{unique_id}.webp"

        # Storage failures are server-side; never surface backend internals to
        # the caller.
        if raise_on_error:
            try:
                obj = await upload_file(optimized_buffer, object_name, content_type)
            except StorageError as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY, detail="Storage backend unavailable"
                ) from exc
        else:
            obj = await upload_file(optimized_buffer, object_name, content_type)

        # Record the object in the system-of-record (owner-scoped, immediately
        # ready, with the content hash so a later identical upload dedupes). If
        # this fails the stored object would be orphaned with no way to ever
        # find or delete it, so roll it back before surfacing a generic 502.
        try:
            record = await store.create(
                owner=owner,
                kind=KIND_IMAGE,
                storage_key=obj.key,
                content_type=content_type,
                size_bytes=obj.size,
                status=STATUS_READY,
                width=width,
                height=height,
                content_hash=content_hash,
                visibility="public",
            )
        except MetadataError as exc:
            logger.error("Failed to record image upload %s: %s", obj.key, exc)
            with contextlib.suppress(StorageError):
                await delete_file(obj.key)
            if not raise_on_error:
                return None
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="Upload could not be completed"
            ) from exc

        source_url = await _image_source_url(obj.key)
        return _image_response(
            record.id,
            width,
            height,
            record.size_bytes,
            source_url,
            custom_width=imgproxy_width,
            custom_height=imgproxy_height,
            custom_fit=imgproxy_fit,
            custom_format=imgproxy_format,
        )
    except HTTPException:
        raise
    except Exception as exc:
        if raise_on_error:
            raise
        logger.error("Failed to process bulk image item: %s", exc)
        return None


@router.post(
    "/upload/image",
    tags=["Uploads"],
    summary="Upload image",
    response_model=ImageUploadResponse,
    response_model_exclude_unset=True,
)
async def upload_image(
    request: Request,
    file: UploadFile = File(...),
    imgproxy_width: int | None = Form(
        default=None, ge=0, le=8192, json_schema_extra={"default": ""}
    ),
    imgproxy_height: int | None = Form(
        default=None, ge=0, le=8192, json_schema_extra={"default": ""}
    ),
    imgproxy_fit: Literal["auto", "fit", "fill", "fill-down", "force"] = Form(
        default="auto", examples=["auto"]
    ),
    imgproxy_format: Literal["webp", "png", "jpg", "jpeg", "avif", "gif"] | None = Form(
        default=None, json_schema_extra={"example": None}
    ),
    optimization: Literal["size", "balanced", "quality"] = Form(
        "balanced", description="Encoding profile for initial image compression"
    ),
    owner: str = Depends(require_image_upload),
):
    file_data = await _read_capped(file, request, settings.MAX_IMAGE_UPLOAD_BYTES)
    return await _process_single_image(
        file_data,
        owner,
        optimization,
        imgproxy_width,
        imgproxy_height,
        imgproxy_fit,
        imgproxy_format,
        raise_on_error=True,
    )


# FastAPI's OpenAPI generation can't natively express `List[UploadFile]` in the
# multipart Swagger schema; this workaround describes the request body by hand
# so the docs UI renders a usable "files" array picker. Module-level so the
# route decorator stays readable.
_BULK_UPLOAD_OPENAPI_EXTRA = {
    "requestBody": {
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "array",
                            "items": {"type": "string", "format": "binary"},
                            "description": "Multiple image files to upload",
                        },
                        "imgproxy_width": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 8192,
                            "nullable": True,
                        },
                        "imgproxy_height": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 8192,
                            "nullable": True,
                        },
                        "imgproxy_fit": {
                            "type": "string",
                            "enum": ["auto", "fit", "fill", "fill-down", "force"],
                            "default": "auto",
                        },
                        "imgproxy_format": {
                            "type": "string",
                            "enum": ["webp", "png", "jpg", "jpeg", "avif", "gif"],
                            "nullable": True,
                        },
                        "optimization": {
                            "type": "string",
                            "enum": ["size", "balanced", "quality"],
                            "default": "balanced",
                        },
                    },
                    "required": ["files"],
                }
            }
        }
    }
}


@router.post(
    "/upload/images",
    tags=["Uploads"],
    summary="Bulk upload images",
    response_model=BulkImageUploadResponse,
    response_model_exclude_unset=True,
    openapi_extra=_BULK_UPLOAD_OPENAPI_EXTRA,
)
async def upload_images(
    request: Request,
    files: Annotated[list[UploadFile], File(description="Multiple image files to upload")],
    imgproxy_width: int | None = Form(
        default=None, ge=0, le=8192, json_schema_extra={"default": ""}
    ),
    imgproxy_height: int | None = Form(
        default=None, ge=0, le=8192, json_schema_extra={"default": ""}
    ),
    imgproxy_fit: Literal["auto", "fit", "fill", "fill-down", "force"] = Form(
        default="auto", examples=["auto"]
    ),
    imgproxy_format: Literal["webp", "png", "jpg", "jpeg", "avif", "gif"] | None = Form(
        default=None, json_schema_extra={"example": None}
    ),
    optimization: Literal["size", "balanced", "quality"] = Form(
        "balanced", description="Encoding profile for initial image compression"
    ),
    owner: str = Depends(require_image_upload),
):
    if not files or len(files) == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No files provided")
    if len(files) > 10:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Maximum of 10 files allowed"
        )

    # Validate size up to 50MB and keep in memory
    MAX_TOTAL_BYTES = 50 * 1024 * 1024
    total_bytes = 0
    files_data = []

    for file in files:
        data = await _read_capped(file, request, MAX_TOTAL_BYTES - total_bytes)
        total_bytes += len(data)
        files_data.append(data)

    results = await asyncio.gather(
        *[
            _process_single_image(
                data,
                owner,
                optimization,
                imgproxy_width,
                imgproxy_height,
                imgproxy_fit,
                imgproxy_format,
                raise_on_error=False,
            )
            for data in files_data
        ]
    )
    items = [r for r in results if r is not None]

    return {"count": len(items), "items": items}


@router.post(
    "/upload/video",
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Uploads"],
    summary="Upload video",
    response_model=VideoUploadResponse,
    response_model_exclude_unset=True,
)
async def upload_video(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    callback_url: Annotated[str | None, Form(json_schema_extra={"example": None})] = None,
    format: Literal["mp4", "webm_vp9", "webm_av1"] = Form("mp4"),
    optimization: Literal["balanced", "quality"] = Form("balanced", description="Encoding profile"),
    start_seconds: float | None = Form(
        None, description="Timestamp to start cropping from", ge=0.0
    ),
    end_seconds: float | None = Form(None, description="Timestamp to stop cropping at", ge=0.0),
    poster_seconds: float | None = Form(
        None, description="Timestamp for poster frame extraction", ge=0.0
    ),
    visibility: Literal["public", "private"] = Form("public"),
    owner: str = Depends(require_video_upload),
):
    # Admit the optional webhook target up front (before ingesting) so a bad or
    # disallowed URL is a clean 400 with nothing staged and no temp to clean up.
    if callback_url is not None:
        try:
            callback_url = validate_callback_url(callback_url)
        except WebhookValidationError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Stream the upload straight to a temp file (bounded memory: one chunk at a
    # time) and hash it incrementally as it lands, instead of buffering the whole
    # body (up to MAX_VIDEO_UPLOAD_BYTES) in RAM. The temp file is removed in the
    # finally, whatever happens below.
    temp_path, size, raw_content_hash = await _stream_capped_to_temp(
        file, request, settings.MAX_VIDEO_UPLOAD_BYTES
    )
    # The deduplication hash must include the processing parameters, otherwise
    # uploading the same video with different formats/cuts would falsely dedupe.
    signature = (
        f"{raw_content_hash}:{format}:{optimization}:{start_seconds}:{end_seconds}:{poster_seconds}"
    )
    content_hash = hashlib.sha256(signature.encode()).hexdigest()
    content_type = file.content_type or "application/octet-stream"
    try:
        # Idempotency (video): on a match against this owner's existing
        # `ready`-or-`processing` video (keyed on the raw input hash just
        # computed), skip staging + enqueue and return the existing job. `ready`
        # -> 200 (already available); `processing` -> 202 (attach to the in-flight
        # compression, don't compress twice). Keyed on the *raw input*, since the
        # compressed output is nondeterministic. A lookup failure must not fail
        # the upload -- fall through and process.
        store = await get_metadata_store()
        try:
            duplicate = await store.find_active_video_by_hash(owner, content_hash)
        except MetadataError as exc:
            logger.warning("Video idempotency lookup failed (processing normally): %s", exc)
            duplicate = None
        if duplicate is not None:
            response.status_code = (
                status.HTTP_200_OK if duplicate.status == STATUS_READY else status.HTTP_202_ACCEPTED
            )
            return {
                "status": "duplicate",
                "id": duplicate.id,
                "task_id": duplicate.task_id,
                "record_status": duplicate.status,
            }

        # Stage the raw upload in storage (streamed from the temp file, never
        # re-buffered); only its key travels through Redis.
        original_filename = file.filename or "video.mp4"
        ext = _sanitize_extension(original_filename)
        raw_key = f"raw/videos/{uuid.uuid4().hex}.{ext}"
        try:
            await upload_file_from_path(temp_path, raw_key, content_type)
        except StorageError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="Storage backend unavailable"
            ) from exc

        # Record the upload as `processing` BEFORE enqueuing, so the worker (which
        # marks it ready by id) can never observe the row before it exists. The
        # raw key is the current authoritative object; the worker swaps it for the
        # compressed key on success. The content hash is stored so a later
        # identical upload dedupes (find_active_video_by_hash above).
        try:
            record = await store.create(
                owner=owner,
                kind=KIND_VIDEO,
                storage_key=raw_key,
                content_type=content_type,
                size_bytes=size,
                status=STATUS_PROCESSING,
                content_hash=content_hash,
                original_filename=original_filename,
                callback_url=callback_url,
                visibility=visibility,
            )
        except MetadataError as exc:
            logger.error("Failed to record video upload %s: %s", raw_key, exc)
            with contextlib.suppress(StorageError):
                await delete_file(raw_key)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="Upload could not be completed"
            ) from exc

        # Enqueue the task with the lightweight key reference + the record id.
        try:
            task = await compress_video_task.kiq(
                raw_storage_key=raw_key,
                original_filename=original_filename,
                upload_id=record.id,
                output_format=format,
                optimization=optimization,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                poster_seconds=poster_seconds,
            )
        except Exception as exc:
            # Enqueue failed (e.g. Redis down): don't leave a row stuck
            # `processing` forever with no task behind it -- roll back the record
            # and the raw object, then surface a generic 502.
            logger.error("Failed to enqueue compression for %s: %s", record.id, exc)
            with contextlib.suppress(MetadataError):
                await store.delete(record.id, owner)
            with contextlib.suppress(StorageError):
                await delete_file(raw_key)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Video processing temporarily unavailable",
            ) from exc

        # Link the record to its task id so GET /tasks/{id} can resolve ownership
        # + existence from the record. Best-effort: the upload is already accepted
        # and the task queued, so a failure to record the task id must not fail the
        # request (the poller would just 404 until a retry sets it).
        with contextlib.suppress(MetadataError):
            await store.set_task_id(record.id, task.task_id)

        return {
            "status": "accepted",
            "id": record.id,
            "task_id": task.task_id,
            "raw_key": raw_key,
        }
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(temp_path)
