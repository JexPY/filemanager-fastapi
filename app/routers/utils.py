import hashlib
import re
from pathlib import Path

from fastapi import HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse, Response

from app.config import settings
from app.services.imgproxy import build_source_url, generate_signed_url
from app.services.storage import StorageError, get_storage


async def _read_capped(file: UploadFile, request: Request, max_bytes: int) -> bytes:
    """Read an upload into memory, aborting safely if it exceeds max_bytes."""
    content = bytearray()
    while chunk := await file.read(1024 * 1024):  # 1MB chunks
        if await request.is_disconnected():
            raise HTTPException(
                status_code=status.HTTP_499_CLIENT_CLOSED_REQUEST, detail="Client disconnected"
            )
        content.extend(chunk)
        if len(content) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File exceeds {max_bytes} bytes",
            )
    return bytes(content)


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")
_MAX_EXTENSION_LENGTH = 8


def _sanitize_extension(filename: str) -> str:
    """Derives a safe storage-key suffix from a client-supplied filename."""
    raw_ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
    cleaned = _NON_ALNUM_RE.sub("", raw_ext.lower())[:_MAX_EXTENSION_LENGTH]
    return cleaned or "bin"


def _public_url(path: str) -> str:
    """Turn an app-relative path (e.g. /share/<token>) into an absolute URL when
    PUBLIC_BASE_URL is configured, otherwise return the path unchanged so the
    client prefixes its own origin."""
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return f"{base}{path}" if base else path


def _storage_public_base_configured() -> bool:
    """Does the current storage backend have a public base URL configured?"""
    if settings.STORAGE_BACKEND == "local":
        return bool(settings.LOCAL_PUBLIC_BASE_URL)
    if settings.STORAGE_BACKEND == "s3":
        return bool(settings.S3_PUBLIC_BASE_URL)
    if settings.STORAGE_BACKEND == "gcp":
        return bool(settings.GCS_PUBLIC_BASE_URL)
    return False


def _assert_safe_media_key(storage_key: str) -> None:
    """Fail the request if a storage key looks like path traversal."""
    if ".." in storage_key or storage_key.startswith("/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid path")


def _xaccel_response(storage_key: str) -> Response:
    """Yield a local video via nginx's X-Accel-Redirect. The app issues the
    header; nginx serves the bytes directly from the volume, natively supporting
    Range/seek. The `media_data` location block must be internal in nginx.conf.
    Path traversal is blocked locally before touching the nginx boundary."""
    _assert_safe_media_key(storage_key)
    response = Response(media_type="video/mp4")
    response.headers["X-Accel-Redirect"] = f"/internal-media/{storage_key}"
    return response


def _local_file_response(storage_key: str) -> Response:
    """Yield a local video via Starlette's FileResponse (which does support Range).
    Used ONLY when LOCAL_MEDIA_SERVE_MODE=direct (i.e. dev without nginx). Prod
    always uses xaccel."""
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
    source_url: str,
    custom_width: int | None = None,
    custom_height: int | None = None,
    custom_fit: str = "auto",
    custom_format: str | None = None,
) -> dict:
    response = {
        "status": "success",
        "id": record_id,
        "dimensions": {"width": width, "height": height},
        "imgproxy_thumbnail_url": generate_signed_url(
            source_url, processing_options="rs:fill:300:300"
        ),
        "imgproxy_optimized_url": generate_signed_url(source_url, processing_options="rs:auto"),
    }

    if custom_width or custom_height or custom_format or custom_fit != "auto":
        cw = custom_width or 0
        ch = custom_height or 0
        processing_options = f"rs:{custom_fit}:{cw}:{ch}"
        response["imgproxy_custom_url"] = generate_signed_url(
            source_url, processing_options=processing_options, format=custom_format
        )

    return response
