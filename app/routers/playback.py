import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials

from app.routers.auth import _resolve_owner_optional, security, verify_token
from app.routers.utils import _image_source_url, _storage_public_base_configured, resolve_playback
from app.services.imgproxy import generate_signed_url
from app.services.metadata import (
    KIND_IMAGE,
    KIND_VIDEO,
    VISIBILITY_PUBLIC,
    MetadataError,
    get_metadata_store,
)
from app.services.storage import StorageError, get_storage

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/files/{file_id}/download",
    tags=["Sharing & Playback"],
    summary="Download / stream video",
)
async def download_file_endpoint(
    file_id: Annotated[str, Path(max_length=64)],
    request: Request,
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
        # Accepts the owner's bearer via header OR ?token= (a <video src> can't
        # set headers), static token or capability JWT alike.
        owner = _resolve_owner_optional(credentials, request)
        if owner is None or owner != record.owner:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    try:
        return await resolve_playback(record.storage_key)
    except StorageError as exc:
        logger.error("Failed to resolve playback for %s: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Playback backend unavailable"
        ) from exc


@router.get(
    "/share/{share_token}",
    tags=["Sharing & Playback"],
    summary="Stream via share link",
)
async def download_via_share(share_token: Annotated[str, Path(max_length=100)]):
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


@router.get(
    "/files/{file_id}/image-url",
    tags=["Sharing & Playback"],
    summary="Generate custom image URL",
)
async def get_custom_image_url(
    file_id: Annotated[str, Path(max_length=64)],
    width: Annotated[int | None, Query(ge=0, le=8192)] = None,
    height: Annotated[int | None, Query(ge=0, le=8192)] = None,
    fit: Literal["auto", "fit", "fill", "fill-down", "force"] = Query(default="auto"),
    format: Literal["webp", "png", "jpg", "jpeg", "avif", "gif"] | None = Query(default="webp"),
    owner: str = Depends(verify_token),
):
    """Dynamically generate a signed imgproxy URL for an uploaded image.
    Owner-scoped (404, not 403, if it isn't the caller's).
    """
    store = await get_metadata_store()
    try:
        record = await store.get(file_id, owner)
    except MetadataError as exc:
        logger.error("Failed to load upload %s for custom url: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if record.kind != KIND_IMAGE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Custom URLs are only for images"
        )

    cw = width or 0
    ch = height or 0
    processing_options = f"rs:{fit}:{cw}:{ch}"
    source_url = await _image_source_url(record.storage_key)
    signed_url = generate_signed_url(
        source_url, processing_options=processing_options, format=format
    )

    return {"id": file_id, "url": signed_url}
