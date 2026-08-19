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
from app.services.file_validation import MIME_OCTET_STREAM, get_content_disposition_type
from app.services.imgproxy import signed_image_url
from app.services.renditions import derive_thumbnail_url
from app.services.storage import StorageError, get_storage, has_public_base_url, public_object_url
from app.urls import public_url

# 499 is nginx's non-standard "client closed request" code, which is what the
# edge proxy in front of this app already logs for an abandoned upload. Starlette
# only defines IANA-registered codes, so `status.HTTP_499_CLIENT_CLOSED_REQUEST`
# does not exist -- referencing it raised AttributeError (a 500) on exactly the
# path meant to return 499. Define it here instead of guessing a nearby standard
# code, so the abort stays distinguishable from a real client or server error.
HTTP_499_CLIENT_CLOSED_REQUEST = 499

_CSP_DEFAULT_NONE = "default-src 'none';"


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


def _assert_safe_media_key(storage_key: str) -> None:
    """Fail the request if a storage key looks like path traversal."""
    if ".." in storage_key or storage_key.startswith("/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid path")


def _sanitize_content_disposition_filename(filename: str) -> str:
    """Strip characters that could break or inject into the Content-Disposition header."""
    cleaned = re.sub(r'[\x00-\x1f\x7f"\\]', "_", filename)
    return cleaned[:255] if cleaned else "download"


def _xaccel_response(
    storage_key: str,
    filename: str | None = None,
    media_type: str = MIME_OCTET_STREAM,
) -> Response:
    """Yield a local file via nginx's X-Accel-Redirect. The app issues the
    header; nginx serves the bytes directly from the volume, natively supporting
    Range/seek. The `media_data` location block must be internal in nginx.conf.
    Path traversal is blocked locally before touching the nginx boundary.
    `filename` sets an appropriate Content-Disposition so the player/browser can
    view displayable files inline or download binary files."""
    _assert_safe_media_key(storage_key)
    response = Response(media_type=media_type)
    response.headers["X-Accel-Redirect"] = f"/internal-media/{storage_key}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = _CSP_DEFAULT_NONE
    if filename:
        safe_name = _sanitize_content_disposition_filename(filename)
        disp_type = get_content_disposition_type(media_type)
        response.headers["Content-Disposition"] = f'{disp_type}; filename="{safe_name}"'
    return response


def _object_xaccel_response(target_url: str, media_type: str = MIME_OCTET_STREAM) -> Response:
    """Stream a private object-store object through nginx, so the signed URL
    never reaches the client.

    The app returns an empty body carrying `X-Accel-Redirect: /internal-object/`
    plus the freshly-signed upstream URL in `X-Object-Target`; nginx reads that
    back out of the upstream response inside an `internal;` location the client
    cannot address, and proxies the bytes (Range included).

    That closes the hole a 302 leaves open. A redirect hands the client a URL
    that is a bearer token for its whole TTL -- leakable through history, a
    Referer header, or a log -- and one that a player caches, so a seek after it
    expires fails. Here there is no client-visible URL at all: ownership is
    re-checked on every single request, and there is no TTL to size.

    The counterpart security property is nginx's `internal;` on that location,
    exactly as with `/internal-media/`.
    """
    # The value is interpolated into nginx's proxy_pass. It is built here from a
    # storage backend, never from client input, but assert the shape anyway --
    # this is the one place a bad value would become an SSRF primitive.
    #
    # This is a scheme *allow-list*, not a URL being fetched over http. Static
    # analysis flags the "http://" literal as insecure use of HTTP; accepting it
    # is deliberate and cannot be dropped: S3-compatible object stores on a
    # private network are addressed over plain HTTP (the Garage fixture is
    # http://garage:3900, as is any in-cluster MinIO/R2 gateway), and this hop is
    # nginx -> object store inside the Docker network, never over the internet.
    # Restricting it to https would break every such deployment while securing
    # nothing that is exposed.
    if not target_url.startswith(("https://", "http://")):  # NOSONAR -- scheme allow-list
        raise StorageError("Refusing to proxy a non-HTTP(S) upstream URL")
    response = Response(media_type=media_type)
    response.headers["X-Accel-Redirect"] = "/internal-object/"
    response.headers["X-Object-Target"] = target_url
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = _CSP_DEFAULT_NONE
    # Deliberately no Content-Disposition here. The signed upstream URL already
    # carries one as a response-header override, and nginx passes the upstream's
    # headers through -- setting it on both sides emitted the header twice
    # (caught by end-to-end verification, not by the unit tests, which only see
    # this response and never nginx's merge of the two).
    return response


def _local_file_response(
    storage_key: str,
    filename: str | None = None,
    media_type: str = MIME_OCTET_STREAM,
) -> Response:
    """Yield a local file via Starlette's FileResponse (which does support Range).
    Used ONLY when LOCAL_MEDIA_SERVE_MODE=direct (i.e. dev without nginx). Prod
    always uses xaccel. `filename` sets an appropriate Content-Disposition, matching
    the X-Accel path."""
    _assert_safe_media_key(storage_key)
    path = Path(settings.LOCAL_STORAGE_DIR) / storage_key
    if not path.is_file():
        raise StorageError(f"Object not found: {storage_key!r}")
    if filename:
        safe_name = _sanitize_content_disposition_filename(filename)
        disp_type = get_content_disposition_type(media_type)
        headers = {"Content-Disposition": f'{disp_type}; filename="{safe_name}"'}
    else:
        headers = {}
    headers["X-Content-Type-Options"] = "nosniff"
    headers["Content-Security-Policy"] = _CSP_DEFAULT_NONE
    return FileResponse(path, media_type=media_type, headers=headers)


async def resolve_playback(
    storage_key: str,
    filename: str | None = None,
    media_type: str = MIME_OCTET_STREAM,
    *,
    private: bool = False,
) -> Response:
    """Resolve a file's byte path, keyed on STORAGE_BACKEND (mirrors
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
        safe_name = _sanitize_content_disposition_filename(filename)
        disp_type = get_content_disposition_type(media_type)
        disposition = f'{disp_type}; filename="{safe_name}"'
    signed = await backend.presigned_get_url(
        storage_key,
        settings.VIDEO_PLAYBACK_URL_TTL_SECONDS,
        content_type=media_type,
        content_disposition=disposition,
    )
    if not signed:
        raise StorageError(f"Backend {backend_name!r} cannot sign a playback URL")
    # Private media streams through nginx by default, so the signed URL stays
    # server-side. Public media keeps redirecting -- the URL is not a secret, and
    # a redirect keeps the bytes (and the bandwidth) off this host entirely.
    if private and settings.PRIVATE_MEDIA_SERVE_MODE == "stream":
        return _object_xaccel_response(signed, media_type=media_type)
    return RedirectResponse(url=signed, status_code=status.HTTP_302_FOUND)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _image_response(
    record_id: str,
    width: int | None,
    height: int | None,
    size_bytes: int | None,
    storage_key: str,
    renditions: dict[str, str] | None = None,
    custom_width: int | None = None,
    custom_height: int | None = None,
    custom_fit: str = "auto",
    custom_format: str | None = None,
    visibility: str = "public",
) -> dict:
    """Shape the POST /upload/image response.

    Takes the storage *key*, not a pre-resolved source URL, so it derives its
    imgproxy URLs through the same `signed_image_url` helper as
    `UploadRecord.to_public`.

    It also mirrors that method's visibility rule: imgproxy URLs are emitted for
    `public` uploads only. An imgproxy URL carries no ownership check and never
    expires, so returning one for a `private` upload handed the owner a
    permanent unauthenticated way to read media they had just marked private --
    and it resolves, since imgproxy reads the shared volume (or a public bucket)
    directly. `GET /files/{id}` already withheld it; this endpoint did not, so
    the two views of the same record disagreed. A private upload is readable
    through `GET /files/{id}/download?rendition=thumb`.
    """
    if visibility == "public" and has_public_base_url():
        main_url = public_object_url(storage_key)
    else:
        main_url = public_url(f"/files/{record_id}/download")

    response = {
        "status": "success",
        "id": record_id,
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / (1024 * 1024), 2) if size_bytes else None,
        "dimensions": {"width": width, "height": height},
        "url": main_url,
    }
    if visibility != "public":
        return response

    if renditions and "thumbnail" in renditions:
        response["thumbnail_url"] = derive_thumbnail_url(storage_key, renditions)

    # A processed image is *always* stored as webp (image_vips.py's
    # validate_and_strip_image encodes to .webp unconditionally), so
    # custom_format="webp" with no width/height/fit is not a customization at
    # all -- it's a no-op imgproxy round trip that re-fetches and re-encodes
    # the already-webp original for zero actual change. A client (or, as
    # observed, Swagger UI's "Try it out" form) that leaves width/height
    # blank but still sends the form's own default format=webp used to
    # trigger this branch and get a redundant custom URL for nothing.
    requests_format_change = custom_format is not None and custom_format != "webp"
    if not (custom_width or custom_height or requests_format_change or custom_fit != "auto"):
        return response

    cw = custom_width or 0
    ch = custom_height or 0
    processing_options = f"rs:{custom_fit}" if not (cw or ch) else f"rs:{custom_fit}:{cw}:{ch}"

    response["custom_url"] = signed_image_url(
        storage_key, processing_options=processing_options, format=custom_format or "webp"
    )

    return response
