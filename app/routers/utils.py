import contextlib
import hashlib
import os
import re
import tempfile
from pathlib import Path

import aiofiles
from fastapi import HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse, Response

from app.config import settings
from app.services.imgproxy import build_source_url, generate_signed_url
from app.services.storage import StorageError, get_storage

# 499 is nginx's non-standard "client closed request" code, which is what the
# edge proxy in front of this app already logs for an abandoned upload. Starlette
# only defines IANA-registered codes, so `status.HTTP_499_CLIENT_CLOSED_REQUEST`
# does not exist -- referencing it raised AttributeError (a 500) on exactly the
# path meant to return 499. Define it here instead of guessing a nearby standard
# code, so the abort stays distinguishable from a real client or server error.
HTTP_499_CLIENT_CLOSED_REQUEST = 499


async def _read_capped(file: UploadFile, request: Request, max_bytes: int) -> bytes:
    """Read an upload into memory, aborting safely if it exceeds max_bytes. Used
    for images (bounded + decoded in memory by pyvips anyway); video streams to
    disk instead via _stream_capped_to_temp."""
    content = bytearray()
    while chunk := await file.read(1024 * 1024):  # 1MB chunks
        if await request.is_disconnected():
            raise HTTPException(
                status_code=HTTP_499_CLIENT_CLOSED_REQUEST, detail="Client disconnected"
            )
        content.extend(chunk)
        if len(content) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"File exceeds {max_bytes} bytes",
            )
    return bytes(content)


async def _stream_capped_to_temp(
    file: UploadFile, request: Request, max_bytes: int
) -> tuple[str, int, str]:
    """Stream an upload straight to a temp file on disk, one chunk at a time, so
    the process never holds the whole body in memory -- the buffered _read_capped
    is fine for small images but not for a hundreds-of-MB video. Enforces the same
    per-chunk size cap and client-disconnect abort, and hashes the input
    incrementally (a rolling sha256) so no separate full-buffer pass is needed.

    Returns (temp_path, size, sha256_hex). On any abort the temp file is removed;
    on success the CALLER owns temp_path and must remove it.
    """
    hasher = hashlib.sha256()
    size = 0
    fd, temp_path = tempfile.mkstemp(prefix="upload_")
    os.close(fd)
    try:
        async with aiofiles.open(temp_path, "wb") as out:
            while chunk := await file.read(1024 * 1024):  # 1MB chunks
                if await request.is_disconnected():
                    raise HTTPException(
                        status_code=HTTP_499_CLIENT_CLOSED_REQUEST,
                        detail="Client disconnected",
                    )
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=f"File exceeds {max_bytes} bytes",
                    )
                await out.write(chunk)
                hasher.update(chunk)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_path)
        raise
    return temp_path, size, hasher.hexdigest()


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")
_MAX_EXTENSION_LENGTH = 8


def _sanitize_extension(filename: str) -> str:
    """Derives a safe storage-key suffix from a client-supplied filename."""
    raw_ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
    cleaned = _NON_ALNUM_RE.sub("", raw_ext.lower())[:_MAX_EXTENSION_LENGTH]
    return cleaned or "bin"


def _public_playback_url_available() -> bool:
    """Whether GET /files/{id}/download may 302 a *public* video straight to a
    stable, directly-servable public_url(key) instead of X-Accel / presigning.

    True only for the object-store backends (s3/gcp) with a *_PUBLIC_BASE_URL --
    a CDN / public-bucket domain that actually serves the object. **Never for
    local**: the media volume is exposed only through nginx's internal X-Accel
    location (never a public path -- that's the visibility security property),
    so a 302 to LOCAL_PUBLIC_BASE_URL/<key> would dead-end on a 404. Local public
    videos are served tokenless via the same X-Accel path as private ones.
    """
    if settings.STORAGE_BACKEND == "s3":
        return bool(settings.S3_PUBLIC_BASE_URL)
    if settings.STORAGE_BACKEND == "gcp":
        return bool(settings.GCS_PUBLIC_BASE_URL)
    return False


def _assert_safe_media_key(storage_key: str) -> None:
    """Fail the request if a storage key looks like path traversal."""
    if ".." in storage_key or storage_key.startswith("/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid path")


def _sanitize_content_disposition_filename(filename: str) -> str:
    """Strip characters that could break or inject into the Content-Disposition header."""
    cleaned = re.sub(r'[\x00-\x1f\x7f"\\]', "_", filename)
    return cleaned[:255] if cleaned else "download"


def _xaccel_response(
    storage_key: str, filename: str | None = None, media_type: str = "video/mp4"
) -> Response:
    """Yield a local video via nginx's X-Accel-Redirect. The app issues the
    header; nginx serves the bytes directly from the volume, natively supporting
    Range/seek. The `media_data` location block must be internal in nginx.conf.
    Path traversal is blocked locally before touching the nginx boundary.
    `filename` sets an `inline` Content-Disposition so the player/browser has a
    sensible name for a "save as" without forcing a download."""
    _assert_safe_media_key(storage_key)
    response = Response(media_type=media_type)
    response.headers["X-Accel-Redirect"] = f"/internal-media/{storage_key}"
    if filename:
        safe_name = _sanitize_content_disposition_filename(filename)
        response.headers["Content-Disposition"] = f'inline; filename="{safe_name}"'
    return response


def _local_file_response(
    storage_key: str, filename: str | None = None, media_type: str = "video/mp4"
) -> Response:
    """Yield a local video via Starlette's FileResponse (which does support Range).
    Used ONLY when LOCAL_MEDIA_SERVE_MODE=direct (i.e. dev without nginx). Prod
    always uses xaccel. `filename` sets an `inline` Content-Disposition, matching
    the X-Accel path."""
    _assert_safe_media_key(storage_key)
    path = Path(settings.LOCAL_STORAGE_DIR) / storage_key
    if not path.is_file():
        raise StorageError(f"Object not found: {storage_key!r}")
    if filename:
        safe_name = _sanitize_content_disposition_filename(filename)
        headers = {"Content-Disposition": f'inline; filename="{safe_name}"'}
    else:
        headers = None
    return FileResponse(path, media_type=media_type, headers=headers)


async def resolve_playback(
    storage_key: str, filename: str | None = None, media_type: str = "video/mp4"
) -> Response:
    """Resolve a video's byte path, keyed on STORAGE_BACKEND (mirrors
    imgproxy.build_source_url's keying): local -> nginx X-Accel (or FileResponse
    in dev); s3/gcp -> 302 to a freshly-minted signed GET URL sized to a viewing
    session. Range works in every path and the app stays out of the byte path
    (nginx or the object store moves the bytes). Raises StorageError (-> 502) if
    the backend can't produce a usable URL.

    `filename` (the original upload name) and `media_type` are threaded onto every
    path, including the object-store 302: they ride as signed response-header
    overrides on the presigned URL, so the *record* decides the Content-Type and
    filename rather than whatever metadata the stored object carries. Previously
    the 302 deferred both to the store, which silently dropped the filename and
    made playback depend on upload-time metadata surviving a backend migration."""
    backend_name = settings.STORAGE_BACKEND
    if backend_name == "local":
        if settings.LOCAL_MEDIA_SERVE_MODE == "direct":
            return _local_file_response(storage_key, filename, media_type=media_type)
        return _xaccel_response(storage_key, filename, media_type=media_type)

    backend = await get_storage()
    disposition = None
    if filename:
        disposition = f'inline; filename="{_sanitize_content_disposition_filename(filename)}"'
    signed = await backend.presigned_get_url(
        storage_key,
        settings.VIDEO_PLAYBACK_URL_TTL_SECONDS,
        content_type=media_type,
        content_disposition=disposition,
    )
    if not signed:
        raise StorageError(f"Backend {backend_name!r} cannot sign a playback URL")
    return RedirectResponse(url=signed, status_code=status.HTTP_302_FOUND)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _image_source_url(storage_key: str) -> str:
    backend = await get_storage()
    presigned_url = await backend.presigned_get_url(storage_key)
    return build_source_url(storage_key, presigned_url or backend.public_url(storage_key))


def _image_response(
    record_id: str,
    width: int | None,
    height: int | None,
    size_bytes: int | None,
    source_url: str,
    custom_width: int | None = None,
    custom_height: int | None = None,
    custom_fit: str = "auto",
    custom_format: str | None = None,
) -> dict:
    response = {
        "status": "success",
        "id": record_id,
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / (1024 * 1024), 2) if size_bytes else None,
        "dimensions": {"width": width, "height": height},
        "imgproxy_thumbnail_url": generate_signed_url(
            source_url, processing_options="rs:fill:300:300", format="webp"
        ),
        "imgproxy_optimized_url": generate_signed_url(
            source_url, processing_options="rs:auto", format="webp"
        ),
    }

    if custom_width or custom_height or custom_format or custom_fit != "auto":
        cw = custom_width or 0
        ch = custom_height or 0
        if cw == 0 and ch == 0:
            processing_options = f"rs:{custom_fit}"
        else:
            processing_options = f"rs:{custom_fit}:{cw}:{ch}"

        response["imgproxy_custom_url"] = generate_signed_url(
            source_url, processing_options=processing_options, format=custom_format
        )

    return response
