import asyncio
import contextlib
import hashlib
import hmac
import logging
import re
import secrets
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.broker import broker
from app.config import settings
from app.services.image_vips import ImageValidationError, validate_and_strip_image
from app.services.imgproxy import build_source_url, generate_signed_url
from app.services.metadata import (
    KIND_IMAGE,
    KIND_VIDEO,
    STATUS_PROCESSING,
    STATUS_READY,
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    MetadataError,
    get_metadata_store,
)
from app.services.qr_generator import generate_qr_image
from app.services.storage import StorageError, delete_file, get_storage, upload_file
from app.services.webhooks import WebhookValidationError, validate_callback_url
from app.tasks import compress_video_task, deliver_webhook_task, generate_poster_task

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBearer(auto_error=False)


def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """Authenticates the bearer token and returns its **owner identity** (not
    the raw token), which callers stamp on records / scope queries by."""
    # auto_error=False so a missing/malformed Authorization header lands here
    # too, instead of Starlette's default 403 -- missing and wrong credentials
    # should both be 401.
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    token = credentials.credentials
    # Compare against every configured secret with no early-out, so timing
    # doesn't leak which (or whether) a token matched. The matched owner is
    # resolved after the full loop; configured secrets are unique.
    matched_owner: str | None = None
    for secret, owner in settings.token_identities.items():
        if hmac.compare_digest(token, secret):
            matched_owner = owner
    if matched_owner is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return matched_owner


async def _read_capped(file: UploadFile, request: Request, max_bytes: int) -> bytes:
    """Reads at most max_bytes+1 bytes, rejecting anything larger with 413.

    Content-Length (when present) is checked up front to reject an honestly
    labeled oversized request without reading anything; the bounded read
    below is the real guarantee since Content-Length can be absent (chunked
    transfer) or wrong.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="File too large",
                )
        except ValueError:
            pass  # malformed header; fall through to the bounded read below

    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="File too large")
    return data


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")
_MAX_EXTENSION_LENGTH = 8
_DEFAULT_EXTENSION = "bin"


def _sanitize_extension(filename: str) -> str:
    """Derives a safe storage-key suffix from a client-supplied filename.

    The raw extension is fully attacker-controlled input: LocalStorage's
    path-traversal guard happens to catch `../`-style payloads for the local
    backend, but S3/GCS have no equivalent guard, so an unsanitized extension
    would let a client control an arbitrary key suffix (arbitrary characters,
    pseudo-directories via `/`, unbounded length) on those backends.
    """
    raw_ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
    cleaned = _NON_ALNUM_RE.sub("", raw_ext.lower())[:_MAX_EXTENSION_LENGTH]
    return cleaned or _DEFAULT_EXTENSION


def _public_url(path: str) -> str:
    """Turn an app-relative path (e.g. /share/<token>) into an absolute URL when
    PUBLIC_BASE_URL is configured, otherwise return the path unchanged so the
    client prefixes its own origin."""
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return f"{base}{path}" if base else path


def _storage_public_base_configured() -> bool:
    """Whether the active backend has a public base URL set -- i.e. a stable,
    embeddable/cacheable object URL (a CDN domain or a deliberately public
    bucket/prefix) exists for `public` media. Without one, `public` playback
    falls back to the same signed-URL delivery as `private` (still tokenless)."""
    backend = settings.STORAGE_BACKEND
    if backend == "local":
        return bool(settings.LOCAL_PUBLIC_BASE_URL)
    if backend == "s3":
        return bool(settings.S3_PUBLIC_BASE_URL)
    if backend == "gcp":
        return bool(settings.GCS_PUBLIC_BASE_URL)
    return False


def _assert_safe_media_key(storage_key: str) -> None:
    """The storage key is UUID-based and already sanitized on write, but it's
    interpolated into an nginx-honoured X-Accel-Redirect / an on-disk path, so
    treat it as untrusted here: no traversal, no absolute paths."""
    if ".." in storage_key or storage_key.startswith("/"):
        raise StorageError(f"Unsafe storage key for playback: {storage_key!r}")


def _xaccel_response(storage_key: str) -> Response:
    """local + xaccel: hand the byte transfer to nginx. The empty-body response's
    X-Accel-Redirect points at nginx's `internal;` /internal-media/ location,
    which serves the file with sendfile (zero-copy) + native Range. The location
    being `internal;` is the load-bearing security property -- it can't be
    requested directly, so this route's auth is never bypassed."""
    _assert_safe_media_key(storage_key)
    return Response(
        status_code=status.HTTP_200_OK,
        headers={
            "X-Accel-Redirect": f"/internal-media/{storage_key}",
            "Content-Type": "video/mp4",
            "Content-Disposition": "inline",
        },
    )


def _local_file_response(storage_key: str) -> Response:
    """local + direct (dev without nginx): Starlette FileResponse, which honours
    Range for on-disk files. Prod uses xaccel; this keeps local dev playable."""
    _assert_safe_media_key(storage_key)
    path = Path(settings.LOCAL_STORAGE_DIR) / storage_key
    if not path.is_file():
        raise StorageError(f"Object not found: {storage_key!r}")
    return FileResponse(path, media_type="video/mp4")


async def resolve_playback(storage_key: str) -> Response:
    """Resolve a video's byte path, keyed on STORAGE_BACKEND (mirrors
    imgproxy.build_source_url's keying): local -> nginx X-Accel (or FileResponse
    in dev); s3/gcp -> 302 to a freshly-minted signed GET URL sized to a viewing
    session. Range works in every path and the app stays out of the byte path
    (nginx or the object store moves the bytes). Raises StorageError (-> 502) if
    the backend can't produce a usable URL."""
    backend_name = settings.STORAGE_BACKEND
    if backend_name == "local":
        if settings.LOCAL_MEDIA_SERVE_MODE == "direct":
            return _local_file_response(storage_key)
        return _xaccel_response(storage_key)

    backend = await get_storage()
    signed = await backend.presigned_get_url(storage_key, settings.VIDEO_PLAYBACK_URL_TTL_SECONDS)
    if not signed:
        raise StorageError(f"Backend {backend_name!r} cannot sign a playback URL")
    return RedirectResponse(url=signed, status_code=status.HTTP_302_FOUND)


@router.post("/generate/qrcode", dependencies=[Depends(verify_token)])
async def generate_qrcode(content: str = Form(..., max_length=settings.MAX_QR_CONTENT_LENGTH)):
    try:
        png_data = await asyncio.to_thread(generate_qr_image, content)
    except ValueError as exc:
        # segno raises ValueError (e.g. DataOverflowError) for content it
        # can't encode -- a client-input problem, not a server fault. The
        # detail is client-safe (segno's own capacity-limit messages don't
        # leak internals), but keep it generic and log the real exception.
        logger.warning("QR generation rejected input: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid QR content"
        ) from exc
    return Response(content=png_data, media_type="image/png")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _image_source_url(storage_key: str) -> str:
    """Rebuild the imgproxy source URL for a stored image from its key alone.

    For a backend that supports presigning (S3/R2/MinIO), prefer a presigned URL
    over the plain object URL: the plain URL is unusable for a private bucket,
    and imgproxy needs a URL it can actually fetch. For the local backend,
    imgproxy has no HTTP path to the API's storage at all -- build_source_url
    swaps in its local:// source scheme instead.
    """
    backend = await get_storage()
    presigned_url = await backend.presigned_get_url(storage_key)
    return build_source_url(storage_key, presigned_url or backend.public_url(storage_key))


def _image_response(record_id: str, width: int | None, height: int | None, source_url: str) -> dict:
    return {
        "status": "success",
        "id": record_id,
        "dimensions": {"width": width, "height": height},
        "raw_url": source_url,
        "imgproxy_thumbnail_url": generate_signed_url(
            source_url, processing_options="rs:fill:300:300"
        ),
        "imgproxy_optimized_url": generate_signed_url(source_url, processing_options="rs:auto"),
    }


@router.post("/upload/image")
async def upload_image(
    request: Request,
    file: UploadFile = File(...),
    owner: str = Depends(verify_token),
):
    file_data = await _read_capped(file, request, settings.MAX_IMAGE_UPLOAD_BYTES)

    # Content hash of the *input* bytes for idempotency. Hashing 25 MB is
    # borderline CPU work, so offload it like the other CPU-bound steps.
    content_hash = await asyncio.to_thread(_sha256_hex, file_data)

    store = await get_metadata_store()
    # Idempotency: the exact same bytes already stored (and ready) by this owner
    # returns the existing record instead of re-decoding/re-encoding/re-storing.
    # Owner-scoped so hashes never collide or leak across tenants. A lookup
    # failure must not fail the upload -- fall through and process normally.
    try:
        existing = await store.find_ready_by_hash(owner, content_hash)
    except MetadataError as exc:
        logger.warning("Idempotency lookup failed (processing normally): %s", exc)
        existing = None
    if existing is not None:
        source_url = await _image_source_url(existing.storage_key)
        return _image_response(existing.id, existing.width, existing.height, source_url)

    # Client-side failures (bad/unsupported image) => 400, generic detail.
    try:
        optimized_buffer, content_type, width, height = await asyncio.to_thread(
            validate_and_strip_image, file_data
        )
    except ImageValidationError as exc:
        logger.warning("Image validation rejected upload: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or unsupported image"
        ) from exc

    unique_id = uuid.uuid4().hex
    object_name = f"images/{unique_id}.webp"

    # Storage failures are server-side; never surface backend internals to the caller.
    try:
        obj = await upload_file(optimized_buffer, object_name, content_type)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Storage backend unavailable"
        ) from exc

    # Record the object in the system-of-record (owner-scoped, immediately
    # ready, with the content hash so a later identical upload dedupes). If this
    # fails the stored object would be orphaned with no way to ever find or
    # delete it, so roll it back before surfacing a generic 502.
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
        )
    except MetadataError as exc:
        logger.error("Failed to record image upload %s: %s", obj.key, exc)
        with contextlib.suppress(StorageError):
            await delete_file(obj.key)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Upload could not be completed"
        ) from exc

    source_url = await _image_source_url(obj.key)
    return _image_response(record.id, width, height, source_url)


@router.post("/upload/video", status_code=status.HTTP_202_ACCEPTED)
async def upload_video(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    callback_url: str | None = Form(default=None),
    owner: str = Depends(verify_token),
):
    file_data = await _read_capped(file, request, settings.MAX_VIDEO_UPLOAD_BYTES)

    # Admit the optional webhook target up front (before staging/enqueue) so a
    # bad or disallowed URL is a clean 400, not a silent non-delivery later.
    if callback_url is not None:
        try:
            callback_url = validate_callback_url(callback_url)
        except WebhookValidationError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Idempotency (video): hash the raw input bytes and, on a match against this
    # owner's existing `ready`-or-`processing` video, skip staging + enqueue and
    # return the existing job. `ready` -> 200 (already available); `processing`
    # -> 202 (attach to the in-flight compression, don't compress twice). Keyed
    # on the *raw input*, since the compressed output is nondeterministic. A
    # lookup failure must not fail the upload -- fall through and process.
    content_hash = await asyncio.to_thread(_sha256_hex, file_data)
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

    # Stage the raw upload in storage; only its key travels through Redis.
    original_filename = file.filename or "video.mp4"
    ext = _sanitize_extension(original_filename)
    raw_key = f"raw/videos/{uuid.uuid4().hex}.{ext}"
    try:
        await upload_file(
            file_data,
            raw_key,
            file.content_type or "application/octet-stream",
        )
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Storage backend unavailable"
        ) from exc

    # Record the upload as `processing` BEFORE enqueuing, so the worker (which
    # marks it ready by id) can never observe the row before it exists. The
    # raw key is the current authoritative object; the worker swaps it for the
    # compressed key on success. The content hash is stored so a later identical
    # upload dedupes (find_active_video_by_hash above).
    try:
        record = await store.create(
            owner=owner,
            kind=KIND_VIDEO,
            storage_key=raw_key,
            content_type=file.content_type or "application/octet-stream",
            size_bytes=len(file_data),
            status=STATUS_PROCESSING,
            content_hash=content_hash,
            original_filename=original_filename,
            callback_url=callback_url,
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
        )
    except Exception as exc:
        # Enqueue failed (e.g. Redis down): don't leave a row stuck `processing`
        # forever with no task behind it -- roll back the record and the raw
        # object, then surface a generic 502.
        logger.error("Failed to enqueue compression for %s: %s", record.id, exc)
        with contextlib.suppress(MetadataError):
            await store.delete(record.id, owner)
        with contextlib.suppress(StorageError):
            await delete_file(raw_key)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Video processing temporarily unavailable",
        ) from exc

    # Link the record to its task id so GET /tasks/{id} can resolve ownership +
    # existence from the record. Best-effort: the upload is already accepted and
    # the task queued, so a failure to record the task id must not fail the
    # request (the poller would just 404 until a retry sets it).
    with contextlib.suppress(MetadataError):
        await store.set_task_id(record.id, task.task_id)

    return {
        "status": "accepted",
        "id": record.id,
        "task_id": task.task_id,
        "raw_key": raw_key,
    }


@router.get("/tasks/{task_id}")
async def get_task_status(task_id: str, owner: str = Depends(verify_token)):
    # Owner-scoped via the uploads record: a task_id that isn't this owner's
    # (or never existed) is a 404, so no one can poll another owner's task and
    # existence never leaks across tenants. The record is also the durable proof
    # the task was issued -- it distinguishes "still running" (record exists,
    # result not ready -> pending) from "never existed" (no record -> 404),
    # which is_result_ready alone cannot.
    store = await get_metadata_store()
    try:
        record = await store.get_by_task_id(task_id, owner)
    except MetadataError as exc:
        logger.error("Failed to resolve task %s: %s", task_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown task id")

    is_ready = await broker.result_backend.is_result_ready(task_id)
    if not is_ready:
        return {"task_id": task_id, "status": "pending"}

    task_result = await broker.result_backend.get_result(task_id)
    if task_result.is_err:
        # The underlying exception (e.g. raw ffmpeg stderr, which can include
        # internal /tmp paths) is server-side detail only -- never echoed to
        # the caller, unlike the rest of this route which was already careful
        # about that (StorageError -> generic 502 above).
        logger.error("Task %s failed: %s", task_id, task_result.error)
        return {"task_id": task_id, "status": "failed", "error": "Video processing failed"}

    return {"task_id": task_id, "status": "completed", "result": task_result.return_value}


@router.get("/files")
async def list_files(
    owner: str = Depends(verify_token),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    kind: str | None = Query(default=None),
):
    """List the caller's uploads, newest first. Owner-scoped: a token only ever
    sees its own records. `kind` optionally filters to 'image' or 'video'."""
    store = await get_metadata_store()
    try:
        records = await store.list(owner, kind=kind, limit=limit, offset=offset)
    except MetadataError as exc:
        logger.error("Failed to list uploads for %s: %s", owner, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    return {
        "files": [record.to_public() for record in records],
        "limit": limit,
        "offset": offset,
    }


@router.get("/files/{file_id}")
async def get_file(file_id: str, owner: str = Depends(verify_token)):
    """Fetch one of the caller's upload records. 404 (not 403) when it isn't
    the caller's, so a record's existence never leaks across owners."""
    store = await get_metadata_store()
    try:
        record = await store.get(file_id, owner)
    except MetadataError as exc:
        logger.error("Failed to load upload %s: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return record.to_public()


class _VisibilityBody(BaseModel):
    visibility: str


@router.patch("/files/{file_id}")
async def set_file_visibility(
    file_id: str, body: _VisibilityBody, owner: str = Depends(verify_token)
):
    """Set a video's playback visibility (`private` | `public`). Owner-scoped
    (404, not 403, for anything that isn't the caller's, so existence never leaks).
    Video-only -- images are served through imgproxy and have no visibility model
    here. `public` makes /files/{id}/download and any share link fetchable without
    a token; `private` restricts /download to the owner.
    """
    if body.visibility not in (VISIBILITY_PRIVATE, VISIBILITY_PUBLIC):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"visibility must be '{VISIBILITY_PRIVATE}' or '{VISIBILITY_PUBLIC}'",
        )
    store = await get_metadata_store()
    try:
        record = await store.get(file_id, owner)
    except MetadataError as exc:
        logger.error("Failed to load upload %s for visibility: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if record.kind != KIND_VIDEO:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Visibility only applies to videos"
        )

    try:
        updated = await store.set_visibility(file_id, owner, body.visibility)
    except MetadataError as exc:
        logger.error("Failed to set visibility on %s: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    # updated is None only on a delete race between the load and the update;
    # treat it as gone.
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return updated.to_public()


@router.post("/files/{file_id}/share")
async def create_share_link(file_id: str, owner: str = Depends(verify_token)):
    """Mint (or rotate) an unlisted, revocable share token for one of the caller's
    videos. A valid token serves the video regardless of visibility via
    GET /share/{token}. This is the ONLY endpoint that returns the token + the
    shareable URL -- it's a secret capability and never appears in to_public()
    (listings/webhooks/GET /files/{id}). Calling again rotates the token, which
    revokes the previous link.
    """
    store = await get_metadata_store()
    try:
        record = await store.get(file_id, owner)
    except MetadataError as exc:
        logger.error("Failed to load upload %s for share: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if record.kind != KIND_VIDEO:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Share links only apply to videos"
        )

    token = secrets.token_urlsafe(32)
    try:
        updated = await store.set_share_token(file_id, owner, token)
    except MetadataError as exc:
        logger.error("Failed to set share token on %s: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return {
        "id": file_id,
        "share_token": token,
        "share_url": _public_url(f"/share/{token}"),
    }


@router.delete("/files/{file_id}/share", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_share_link(file_id: str, owner: str = Depends(verify_token)):
    """Revoke the caller's video share link (clears the token; the old URL now
    404s). Owner-scoped. Idempotent -- revoking when there's no token is a no-op
    204, but a non-owner / unknown id is still 404."""
    store = await get_metadata_store()
    try:
        record = await store.get(file_id, owner)
    except MetadataError as exc:
        logger.error("Failed to load upload %s for share revoke: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    try:
        await store.clear_share_token(file_id, owner)
    except MetadataError as exc:
        logger.error("Failed to clear share token on %s: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _resolve_owner_optional(credentials: HTTPAuthorizationCredentials | None) -> str | None:
    """Owner identity for a bearer token if one was presented and valid, else None.
    The download endpoint needs this (not the verify_token dependency) because a
    `public` video is fetchable with no token at all."""
    if credentials is None:
        return None
    token = credentials.credentials
    matched_owner: str | None = None
    for secret, owner in settings.token_identities.items():
        if hmac.compare_digest(token, secret):
            matched_owner = owner
    return matched_owner


@router.get("/files/{file_id}/download")
async def download_file_endpoint(
    file_id: str,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """The permanent, backend-agnostic playback URL -- the one clients embed.
    Visibility decides the auth *and* the URL form:

    * `public` -> if a public base URL is configured for the backend, 302 to the
      stable `public_url(key)` (no token, cacheable behind a CDN, app out of the
      read path); otherwise fall back to the signed-URL/resolver path, still
      tokenless. No per-request presigning for public media -- a unique signature
      would defeat CDN caching.
    * `private` -> requires the owner bearer; a non-owner or missing token gets
      404 (not 403), matching the rest of the owner-scoping so existence never
      leaks. Then resolved per backend (local X-Accel / s3 presigned / gcs signed).

    Video-scoped (kind != video -> 400). The API only *issues* the redirect/header;
    it never proxies the bytes for the public/feed case.
    """
    store = await get_metadata_store()
    try:
        record = await store.get_by_id(file_id)
    except MetadataError as exc:
        logger.error("Failed to load upload %s for download: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if record.kind != KIND_VIDEO:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Downloads are only for videos"
        )

    if record.visibility == VISIBILITY_PUBLIC:
        # Stable, embeddable URL when one exists; else tokenless signed delivery.
        if _storage_public_base_configured():
            backend = await get_storage()
            return RedirectResponse(
                url=backend.public_url(record.storage_key), status_code=status.HTTP_302_FOUND
            )
    else:
        # Private: owner-only, and a non-owner is a 404 (existence never leaks).
        owner = _resolve_owner_optional(credentials)
        if owner is None or owner != record.owner:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    try:
        return await resolve_playback(record.storage_key)
    except StorageError as exc:
        logger.error("Failed to resolve playback for %s: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Playback backend unavailable"
        ) from exc


@router.get("/share/{share_token}")
async def download_via_share(share_token: str):
    """Permanent capability link: a valid share token serves the video regardless
    of its `visibility`, with no bearer token. Unguessable + revocable (it lives
    in our namespace, so it has an off switch, unlike a never-expiry presigned
    URL). Unknown token -> 404."""
    store = await get_metadata_store()
    try:
        record = await store.get_by_share_token(share_token)
    except MetadataError as exc:
        logger.error("Failed to resolve share token: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    if record is None or record.kind != KIND_VIDEO:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    try:
        return await resolve_playback(record.storage_key)
    except StorageError as exc:
        logger.error("Failed to resolve playback via share token: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Playback backend unavailable"
        ) from exc


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_upload(file_id: str, owner: str = Depends(verify_token)):
    """Delete one of the caller's uploads: its storage object, then its record.
    Owner-scoped (404 for anything that isn't the caller's, so no one can probe
    or delete another owner's objects).

    Deletes the object first and only drops the row on success, so a transient
    storage failure leaves the record intact and retryable rather than stranding
    an object with no record. For a video still `processing`, the current object
    is the raw upload; the worker's mark_ready then finds no row and discards its
    output, so no orphan is left either way.
    """
    store = await get_metadata_store()
    try:
        record = await store.get(file_id, owner)
    except MetadataError as exc:
        logger.error("Failed to load upload %s for delete: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    try:
        await delete_file(record.storage_key)
    except StorageError as exc:
        logger.error("Failed to delete object %s: %s", record.storage_key, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Storage backend unavailable"
        ) from exc

    try:
        await store.delete(file_id, owner)
    except MetadataError as exc:
        logger.error("Failed to delete record %s: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc

    # Cascade: a video's poster is a derived object with no standalone meaning,
    # so remove it with its parent. Best-effort -- a poster-cleanup failure must
    # not fail the delete that already succeeded (it would just leave the poster
    # image row/object, findable and deletable on its own).
    if record.kind == KIND_VIDEO and record.poster_upload_id:
        with contextlib.suppress(StorageError, MetadataError):
            poster = await store.get(record.poster_upload_id, owner)
            if poster is not None:
                await delete_file(poster.storage_key)
                await store.delete(poster.id, owner)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/files/{file_id}/poster", status_code=status.HTTP_202_ACCEPTED)
async def generate_poster(
    file_id: str,
    response: Response,
    owner: str = Depends(verify_token),
    at_seconds: float | None = Form(default=None),
):
    """Generate a poster (thumbnail) image for one of the caller's *ready*
    videos, on request. Owner-scoped. Idempotent on the happy path: if a poster
    already exists it's returned inline (200) rather than regenerated. Otherwise
    a poster task is enqueued (202) and the client polls GET /files/{file_id}
    until `poster_upload_id` is set (then GET /files/{poster_id} for the image).

    `at_seconds` optionally picks the frame timestamp; the default is ~10% into
    the clip (avoiding a black lead-in frame). Two simultaneous requests can each
    generate a poster (best-effort, like the upload-idempotency paths); the last
    link wins and the loser is a harmless standalone image row.
    """
    store = await get_metadata_store()
    try:
        record = await store.get(file_id, owner)
    except MetadataError as exc:
        logger.error("Failed to load upload %s for poster: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if record.kind != KIND_VIDEO:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Posters are only for videos"
        )
    if record.status != STATUS_READY:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Video is not ready")
    if at_seconds is not None and at_seconds < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="at_seconds must be non-negative"
        )

    # Already has a live poster -> return it, don't regenerate. (If the linked
    # poster row was deleted out from under the video, fall through and remake.)
    if record.poster_upload_id is not None:
        existing = await store.get(record.poster_upload_id, owner)
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            return {"status": "ready", "video_id": file_id, "poster": existing.to_public()}

    try:
        task = await generate_poster_task.kiq(upload_id=file_id, at_seconds=at_seconds)
    except Exception as exc:
        logger.error("Failed to enqueue poster generation for %s: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Poster generation temporarily unavailable",
        ) from exc

    return {
        "status": "accepted",
        "video_id": file_id,
        "task_id": task.task_id,
        "poll": f"/files/{file_id}",
    }


@router.post("/files/{file_id}/redeliver", status_code=status.HTTP_202_ACCEPTED)
async def redeliver_webhook(file_id: str, owner: str = Depends(verify_token)):
    """Re-enqueue the terminal-state webhook for one of the caller's uploads --
    the manual replay path for a dead-lettered (exhausted) delivery. Owner-scoped.
    Re-sends the current terminal event (`video.completed`/`video.failed`) with
    the same stable X-Webhook-Id, so a receiver that already saw it can dedupe.
    """
    store = await get_metadata_store()
    try:
        record = await store.get(file_id, owner)
    except MetadataError as exc:
        logger.error("Failed to load upload %s for redeliver: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not settings.webhooks_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhooks are not enabled on this server",
        )
    if not record.callback_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No callback_url is configured for this upload",
        )
    if record.status == STATUS_PROCESSING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Video is still processing; no terminal event to deliver yet",
        )

    event = "video.completed" if record.status == STATUS_READY else "video.failed"
    try:
        task = await deliver_webhook_task.kiq(upload_id=file_id, event=event)
    except Exception as exc:
        logger.error("Failed to enqueue webhook redelivery for %s: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Webhook redelivery temporarily unavailable",
        ) from exc

    return {"status": "accepted", "id": file_id, "event": event, "task_id": task.task_id}
