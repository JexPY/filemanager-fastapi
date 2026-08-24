import asyncio
import contextlib
import hashlib
import logging
import os
import uuid
import weakref
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)

from app.config import settings
from app.routers.auth import require_scopes
from app.routers.utils import (
    _image_response,
    _read_capped,
    _sanitize_extension,
    _sanitize_filename,
    _sha256_hex,
    _stream_capped_to_temp,
)
from app.schemas import (
    BulkImageUploadResponse,
    FileUploadResponse,
    ImageUploadResponse,
    VideoUploadResponse,
)
from app.services.file_validation import (
    MIME_IMAGE_WEBP,
    MIME_OCTET_STREAM,
    MIME_VIDEO_MP4,
    MIME_VIDEO_OGG,
    MIME_VIDEO_QUICKTIME,
    MIME_VIDEO_WEBM,
    FileValidationError,
    validate_file_from_path,
)
from app.services.image_vips import ImageValidationError, validate_and_strip_image
from app.services.metadata import (
    KIND_FILE,
    KIND_IMAGE,
    KIND_VIDEO,
    STATUS_PROCESSING,
    STATUS_READY,
    MetadataError,
    get_metadata_store,
)
from app.services.renditions import derive_rendition_key
from app.services.storage import (
    StorageError,
    StorageObject,
    delete_file,
    storage_prefix,
    upload_file,
    upload_file_from_path,
)
from app.services.webhooks import WebhookValidationError, validate_callback_url
from app.tasks import compress_video_task
from app.urls import public_url

logger = logging.getLogger(__name__)
router = APIRouter()

DETAIL_STORAGE_UNAVAILABLE = "Storage backend unavailable"
DETAIL_UPLOAD_FAILED = "Upload could not be completed"


@dataclass(frozen=True)
class _ProcessedImageData:
    buffer: bytes
    content_type: str
    width: int
    height: int
    renditions_buffers: dict[str, bytes]
    content_hash: str
    original_filename: str | None = None


@dataclass(frozen=True)
class _CustomImageParams:
    width: int | None = None
    height: int | None = None
    fit: str = "auto"
    format: str | None = None


_ALLOWED_VIDEO_CONTENT_TYPES = frozenset(
    {
        MIME_VIDEO_MP4,
        MIME_VIDEO_WEBM,
        MIME_VIDEO_QUICKTIME,
        "video/x-matroska",
        "video/x-msvideo",
        "video/mpeg",
        MIME_VIDEO_OGG,
        "video/3gpp",
        MIME_OCTET_STREAM,  # keep as fallback — browsers often send this
    }
)

_BULK_IMAGE_CONCURRENCY_LIMIT = 4
# Keyed by event loop (WeakKeyDictionary so an entry is dropped once its loop
# is garbage collected), one asyncio.Semaphore per loop -- not a single
# module-level Semaphore(4). CPython's asyncio primitives bind to whichever
# running loop first uses them and raise RuntimeError if a second loop in the
# same process ever touches them (e.g. a test suite creating a fresh loop per
# test function hitting this route from two different tests). A normal
# single-worker uvicorn/gunicorn deployment has exactly one loop for its
# whole process lifetime, so this never actually bit in production -- fixed
# anyway since it's a small, self-contained, standard-pattern change.
_bulk_image_semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _bulk_image_concurrency() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _bulk_image_semaphores.get(loop)
    if sem is None:
        sem = _bulk_image_semaphores[loop] = asyncio.Semaphore(_BULK_IMAGE_CONCURRENCY_LIMIT)
    return sem


# Scope-gated auth: a static master token passes unconditionally (full access);
# a capability JWT must carry the matching upload scope, else 403. Built once at
# import so each route reuses one stable dependency callable.
require_image_upload = require_scopes("upload:image")
require_video_upload = require_scopes("upload:video")
require_file_upload = require_scopes("upload:file")


async def _process_single_image(
    file_data: bytes,
    owner: str,
    optimization: str,
    width: int | None,
    height: int | None,
    fit: str,
    format: str | None,
    *,
    thumbnail: bool = False,
    visibility: str = "public",
    raise_on_error: bool,
    original_filename: str | None = None,
) -> dict:
    """Process one image upload end-to-end: sha256 hash -> dedup check ->
    validate/strip -> store -> record. If raise_on_error=True (single upload
    endpoint), propagates HTTPException on client/server failures. If False
    (bulk endpoint), returns an error dict on failure so the batch continues. Never
    leaks storage internals to the caller."""
    try:
        # Content hash of the *input* bytes for idempotency. Hashing 25 MB is
        # borderline CPU work, so offload it like the other CPU-bound steps.
        # The processing parameters are folded in, so the same bytes requested
        # with different options are correctly treated as distinct uploads.
        # `visibility` and `thumbnail` are part of that on purpose: without them,
        # re-uploading with different options would dedupe onto the existing
        # record and silently ignore the caller's intent.
        input_hash = await asyncio.to_thread(_sha256_hex, file_data)
        signature = f"{input_hash}:{optimization}:{visibility}:{thumbnail}"
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
            return _image_response(
                existing.id,
                existing.width,
                existing.height,
                existing.size_bytes,
                existing.storage_key,
                renditions=existing.renditions,
                include_thumbnail=thumbnail,
                custom_width=width,
                custom_height=height,
                custom_fit=fit,
                custom_format=format,
                visibility=existing.visibility,
                original_filename=original_filename,
            )

        # Client-side failures (bad/unsupported image) => 400, generic detail.
        try:
            (
                optimized_buffer,
                content_type,
                img_width,
                img_height,
                renditions_buffers,
            ) = await asyncio.to_thread(
                validate_and_strip_image,
                file_data,
                optimization,
                generate_renditions=thumbnail,
            )
        except ImageValidationError as exc:
            logger.warning("Image validation rejected upload: %s", exc)
            if not raise_on_error:
                return {
                    "status": "error",
                    "code": "invalid_image",
                    "message": "Invalid or unsupported image",
                    "original_filename": original_filename,
                }
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or unsupported image"
            ) from exc

        unique_id = uuid.uuid4().hex
        prefix = storage_prefix("images", visibility)
        object_name = f"{prefix}/{unique_id}.webp"
        img_data = _ProcessedImageData(
            buffer=optimized_buffer,
            content_type=content_type,
            width=img_width,
            height=img_height,
            renditions_buffers=renditions_buffers,
            content_hash=content_hash,
            original_filename=original_filename,
        )
        custom_params = _CustomImageParams(
            width=width,
            height=height,
            fit=fit,
            format=format,
        )

        return await _store_and_record_image(
            store,
            owner,
            object_name,
            img_data,
            custom_params,
            include_thumbnail=thumbnail,
            visibility=visibility,
            raise_on_error=raise_on_error,
        )

    except HTTPException:
        raise
    except Exception:
        if raise_on_error:
            raise
        logger.exception("Failed to process bulk image item")
        return {
            "status": "error",
            "code": "processing_failed",
            "message": "Failed to process image",
            "original_filename": original_filename,
        }


async def _cleanup_uploaded_keys(uploaded_keys: list[str]) -> None:
    for k in uploaded_keys:
        with contextlib.suppress(StorageError):
            await delete_file(k)


async def _safe_upload_file(
    buffer: bytes, key: str, content_type: str, *, raise_on_error: bool
) -> StorageObject:
    try:
        return await upload_file(buffer, key, content_type)
    except StorageError as exc:
        if raise_on_error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=DETAIL_STORAGE_UNAVAILABLE,
            ) from exc
        raise


async def _upload_image_renditions(
    object_name: str,
    renditions_buffers: dict[str, bytes],
    raise_on_error: bool,
    uploaded_keys: list[str],
) -> dict[str, str]:
    renditions_dict: dict[str, str] = {}
    for rend_name, rend_bytes in renditions_buffers.items():
        rend_key = derive_rendition_key(object_name, rend_name)
        r_obj = await _safe_upload_file(
            rend_bytes, rend_key, MIME_IMAGE_WEBP, raise_on_error=raise_on_error
        )
        uploaded_keys.append(r_obj.key)
        renditions_dict[rend_name] = r_obj.key
    return renditions_dict


async def _create_image_record(
    store,
    owner: str,
    obj_key: str,
    obj_size: int,
    img_data: _ProcessedImageData,
    visibility: str,
    renditions_dict: dict[str, str],
    raise_on_error: bool,
    uploaded_keys: list[str],
):
    try:
        return await store.create(
            owner=owner,
            kind=KIND_IMAGE,
            storage_key=obj_key,
            content_type=img_data.content_type,
            size_bytes=obj_size,
            status=STATUS_READY,
            width=img_data.width,
            height=img_data.height,
            content_hash=img_data.content_hash,
            visibility=visibility,
            renditions=renditions_dict,
            original_filename=img_data.original_filename,
        )
    except MetadataError as exc:
        logger.exception("Failed to record image upload")
        await _cleanup_uploaded_keys(uploaded_keys)
        if not raise_on_error:
            return None
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=DETAIL_UPLOAD_FAILED
        ) from exc


async def _store_and_record_image(
    store,
    owner: str,
    object_name: str,
    img_data: _ProcessedImageData,
    custom_params: _CustomImageParams,
    *,
    include_thumbnail: bool = False,
    visibility: str = "public",
    raise_on_error: bool,
) -> dict:
    uploaded_keys: list[str] = []
    try:
        obj = await _safe_upload_file(
            img_data.buffer, object_name, img_data.content_type, raise_on_error=raise_on_error
        )
        uploaded_keys.append(obj.key)

        # Upload materialized renditions (same pass as main image)
        renditions_dict = await _upload_image_renditions(
            object_name, img_data.renditions_buffers, raise_on_error, uploaded_keys
        )

        record = await _create_image_record(
            store,
            owner,
            obj.key,
            obj.size,
            img_data,
            visibility,
            renditions_dict,
            raise_on_error,
            uploaded_keys,
        )
        if record is None:
            return {
                "status": "error",
                "code": "processing_failed",
                "message": "Failed to record image metadata",
                "original_filename": img_data.original_filename,
            }

        return _image_response(
            record.id,
            record.width,
            record.height,
            record.size_bytes,
            obj.key,
            renditions=renditions_dict,
            include_thumbnail=include_thumbnail,
            custom_width=custom_params.width,
            custom_height=custom_params.height,
            custom_fit=custom_params.fit,
            custom_format=custom_params.format,
            visibility=record.visibility,
            original_filename=img_data.original_filename,
        )

    except HTTPException:
        await _cleanup_uploaded_keys(uploaded_keys)
        raise
    except Exception:
        await _cleanup_uploaded_keys(uploaded_keys)
        if raise_on_error:
            raise
        logger.exception("Failed to store and record image")
        return {
            "status": "error",
            "code": "processing_failed",
            "message": "Failed to store image",
            "original_filename": img_data.original_filename,
        }


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
    thumbnail: bool = Query(default=False, description="Generate and return a thumbnail URL"),
    width: int | None = Form(default=None, ge=0, le=8192, json_schema_extra={"default": ""}),
    height: int | None = Form(default=None, ge=0, le=8192, json_schema_extra={"default": ""}),
    fit: Literal["auto", "fit", "fill", "fill-down", "force"] = Form(
        default="auto", examples=["auto"]
    ),
    format: Literal["webp", "png", "jpg", "jpeg", "avif", "gif"] | None = Form(
        default=None, json_schema_extra={"example": None}
    ),
    optimization: Literal["size", "balanced", "quality"] = Form(
        "balanced", description="Encoding profile for initial image compression"
    ),
    visibility: Literal["public", "private"] = Form("public"),
    owner: str = Depends(require_image_upload),
):
    sanitized_filename = _sanitize_filename(file.filename)
    file_data = await _read_capped(file, request, settings.MAX_IMAGE_UPLOAD_BYTES)
    return await _process_single_image(
        file_data,
        owner,
        optimization,
        width,
        height,
        fit,
        format,
        thumbnail=thumbnail,
        visibility=visibility,
        raise_on_error=True,
        original_filename=sanitized_filename,
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
                        "width": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 8192,
                            "nullable": True,
                        },
                        "height": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 8192,
                            "nullable": True,
                        },
                        "fit": {
                            "type": "string",
                            "enum": ["auto", "fit", "fill", "fill-down", "force"],
                            "default": "auto",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["webp", "png", "jpg", "jpeg", "avif", "gif"],
                            "nullable": True,
                        },
                        "optimization": {
                            "type": "string",
                            "enum": ["size", "balanced", "quality"],
                            "default": "balanced",
                        },
                        "visibility": {
                            "type": "string",
                            "enum": ["public", "private"],
                            "default": "public",
                        },
                    },
                    "required": ["files"],
                }
            }
        }
    }
}


async def _process_single_image_throttled(*args: Any, **kwargs: Any) -> dict:
    async with _bulk_image_concurrency():
        return await _process_single_image(*args, **kwargs)


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
    thumbnail: bool = Query(default=False, description="Generate and return thumbnail URLs"),
    width: int | None = Form(default=None, ge=0, le=8192, json_schema_extra={"default": ""}),
    height: int | None = Form(default=None, ge=0, le=8192, json_schema_extra={"default": ""}),
    fit: Literal["auto", "fit", "fill", "fill-down", "force"] = Form(
        default="auto", examples=["auto"]
    ),
    format: Literal["webp", "png", "jpg", "jpeg", "avif", "gif"] | None = Form(
        default=None, json_schema_extra={"example": None}
    ),
    optimization: Literal["size", "balanced", "quality"] = Form(
        "balanced", description="Encoding profile for initial image compression"
    ),
    visibility: Literal["public", "private"] = Form("public"),
    owner: str = Depends(require_image_upload),
):
    if not files or len(files) == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No files provided")
    if len(files) > 10:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Maximum of 10 files allowed"
        )

    # Read each file independently so an oversized/failed file does not abort the batch.
    # We maintain exact positional alignment across all slots and bound total heap usage.
    read_results: list[bytes | dict] = []
    sanitized_filenames: list[str | None] = []
    total_bytes = 0

    for file in files:
        sanitized_name = _sanitize_filename(file.filename)
        sanitized_filenames.append(sanitized_name)

        if total_bytes >= settings.MAX_BULK_UPLOAD_TOTAL_BYTES:
            read_results.append(
                {
                    "status": "error",
                    "code": "batch_too_large",
                    "message": (
                        f"Bulk upload batch exceeds aggregate size limit "
                        f"({settings.MAX_BULK_UPLOAD_TOTAL_BYTES} bytes)"
                    ),
                    "original_filename": sanitized_name,
                }
            )
            continue

        try:
            data = await _read_capped(file, request, settings.MAX_IMAGE_UPLOAD_BYTES)
            total_bytes += len(data)
            read_results.append(data)
        except HTTPException as exc:
            if exc.status_code == status.HTTP_413_CONTENT_TOO_LARGE:
                read_results.append(
                    {
                        "status": "error",
                        "code": "too_large",
                        "message": (
                            f"File exceeds maximum allowed size "
                            f"({settings.MAX_IMAGE_UPLOAD_BYTES} bytes)"
                        ),
                        "original_filename": sanitized_name,
                    }
                )
            else:
                raise
        except Exception:
            logger.exception("Failed to read upload file stream for %s", sanitized_name)
            read_results.append(
                {
                    "status": "error",
                    "code": "processing_failed",
                    "message": "Failed to read upload stream",
                    "original_filename": sanitized_name,
                }
            )

    # Prepare async processing tasks for validly read files
    tasks = []
    task_indices: list[int] = []

    for idx, item in enumerate(read_results):
        if isinstance(item, bytes):
            tasks.append(
                _process_single_image_throttled(
                    item,
                    owner,
                    optimization,
                    width,
                    height,
                    fit,
                    format,
                    thumbnail=thumbnail,
                    visibility=visibility,
                    raise_on_error=False,
                    original_filename=sanitized_filenames[idx],
                )
            )
            task_indices.append(idx)

    processed_results = await asyncio.gather(*tasks) if tasks else []

    # Map processed items back into their exact original slot indices
    final_items: list[dict] = []
    task_map = dict(zip(task_indices, processed_results, strict=True))

    for idx, item in enumerate(read_results):
        if idx in task_map:
            final_items.append(task_map[idx])
        else:
            assert isinstance(item, dict)
            final_items.append(item)

    succeeded_count = sum(1 for it in final_items if it.get("status") == "success")
    failed_count = sum(1 for it in final_items if it.get("status") == "error")

    return {
        "succeeded": succeeded_count,
        "failed": failed_count,
        "total": len(final_items),
        "items": final_items,
    }


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
            callback_url = await validate_callback_url(callback_url)
        except WebhookValidationError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    content_type = file.content_type or "application/octet-stream"
    if content_type not in _ALLOWED_VIDEO_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported video format"
        )

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
        prefix = storage_prefix("raw/videos", visibility)
        raw_key = f"{prefix}/{uuid.uuid4().hex}.{ext}"
        try:
            await upload_file_from_path(temp_path, raw_key, content_type)
        except StorageError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=DETAIL_STORAGE_UNAVAILABLE
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
            logger.exception("Failed to record video upload")
            with contextlib.suppress(StorageError):
                await delete_file(raw_key)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=DETAIL_UPLOAD_FAILED
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
            logger.exception("Failed to enqueue video compression task")
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
        }
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(temp_path)


@router.post(
    "/upload/file",
    tags=["Uploads"],
    summary="Upload generic file",
    response_model=FileUploadResponse,
    response_model_exclude_unset=True,
)
async def upload_generic_file(
    request: Request,
    file: UploadFile = File(...),
    visibility: Literal["public", "private"] = Form("public"),
    owner: str = Depends(require_file_upload),
):
    # Stream upload straight to temp file (bounded memory: one chunk at a time)
    # and compute its rolling sha256.
    temp_path, _, raw_content_hash = await _stream_capped_to_temp(
        file, request, settings.MAX_FILE_UPLOAD_BYTES
    )
    try:
        try:
            content_type = await asyncio.to_thread(
                validate_file_from_path,
                temp_path,
                declared_content_type=file.content_type,
                filename=file.filename,
            )
        except FileValidationError as exc:
            logger.warning("File validation rejected upload: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

        # The deduplication hash includes visibility so identical bytes uploaded
        # with different visibilities are treated as distinct records.
        signature = f"{raw_content_hash}:{visibility}"
        content_hash = hashlib.sha256(signature.encode()).hexdigest()

        store = await get_metadata_store()
        try:
            existing = await store.find_ready_by_hash(owner, content_hash)
        except MetadataError as exc:
            logger.warning("File idempotency lookup failed (processing normally): %s", exc)
            existing = None

        if existing is not None:
            return {
                "status": "success",
                "id": existing.id,
                "kind": existing.kind,
                "content_type": existing.content_type,
                "size_bytes": existing.size_bytes,
                "size_mb": (
                    round(existing.size_bytes / (1024 * 1024), 2) if existing.size_bytes else None
                ),
                "original_filename": existing.original_filename,
                "visibility": existing.visibility,
                "url": public_url(f"/files/{existing.id}/download"),
            }

        original_filename = file.filename or "file.bin"
        ext = _sanitize_extension(original_filename)
        prefix = storage_prefix("files", visibility)
        storage_key = f"{prefix}/{uuid.uuid4().hex}.{ext}"

        try:
            obj = await upload_file_from_path(temp_path, storage_key, content_type)
        except StorageError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=DETAIL_STORAGE_UNAVAILABLE,
            ) from exc

        try:
            record = await store.create(
                owner=owner,
                kind=KIND_FILE,
                storage_key=obj.key,
                content_type=content_type,
                size_bytes=obj.size,
                status=STATUS_READY,
                content_hash=content_hash,
                original_filename=original_filename,
                visibility=visibility,
            )
        except MetadataError as exc:
            logger.exception("Failed to record file upload")
            with contextlib.suppress(StorageError):
                await delete_file(storage_key)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=DETAIL_UPLOAD_FAILED,
            ) from exc

        return {
            "status": "success",
            "id": record.id,
            "kind": record.kind,
            "content_type": record.content_type,
            "size_bytes": record.size_bytes,
            "size_mb": (round(record.size_bytes / (1024 * 1024), 2) if record.size_bytes else None),
            "original_filename": record.original_filename,
            "visibility": record.visibility,
            "url": public_url(f"/files/{record.id}/download"),
        }
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(temp_path)
