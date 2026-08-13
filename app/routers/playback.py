import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials

from app.routers.auth import _resolve_owner_optional, security
from app.routers.utils import _public_playback_url_available, resolve_playback
from app.services.metadata import (
    KIND_VIDEO,
    VISIBILITY_PUBLIC,
    MetadataError,
    get_metadata_store,
)
from app.services.storage import StorageError, get_storage

logger = logging.getLogger(__name__)
router = APIRouter()

_NOT_FOUND_DETAIL = "Not found"


@router.get(
    "/files/{file_id}/download",
    tags=["Sharing & Playback"],
    summary="Get video stream redirect",
    response_class=RedirectResponse,
)
async def stream_video(
    file_id: Annotated[str, Path(max_length=64)],
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
):
    """The permanent, backend-agnostic playback URL -- the one clients embed.
    Visibility decides the auth *and* the URL form:

    * `public` -> tokenless. On an object store (s3/gcp) with a public/CDN base,
      302 to the stable `public_url(key)` (cacheable behind a CDN, app out of the
      read path; no per-request presigning, which would defeat that caching). On
      local, always the tokenless X-Accel path -- the media volume has no public
      URL to redirect to.
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
        logger.exception("Failed to load upload for download")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
    if record.kind != KIND_VIDEO:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Downloads are only for videos"
        )

    if record.visibility == VISIBILITY_PUBLIC:
        # Object stores (s3/gcp) with a public/CDN base -> a stable, embeddable
        # 302 to public_url(key). NOT local: its media volume is served only via
        # nginx's internal X-Accel location, so a 302 to LOCAL_PUBLIC_BASE_URL/<key>
        # would dead-end -- local public videos fall through to the tokenless
        # X-Accel path below (resolve_playback), same as private ones.
        if _public_playback_url_available():
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
        return await resolve_playback(
            record.storage_key, filename=record.original_filename or "video.mp4"
        )
    except StorageError as exc:
        logger.exception("Failed to resolve playback")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Playback backend unavailable"
        ) from exc


@router.get(
    "/share/{share_token}",
    tags=["Sharing & Playback"],
    summary="Stream via share link",
    response_class=RedirectResponse,
)
async def play_shared_video(share_token: Annotated[str, Path(max_length=128)]):
    """Public video streaming via an unlisted share token. Does NOT require
    a bearer token -- the share token *is* the capability. 404 on revoked/unknown."""
    store = await get_metadata_store()
    try:
        record = await store.get_by_share_token(share_token)
    except MetadataError as exc:
        logger.exception("Failed to load share token")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc

    if record is None or record.kind != KIND_VIDEO:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)

    try:
        return await resolve_playback(
            record.storage_key, filename=record.original_filename or "video.mp4"
        )
    except StorageError as exc:
        logger.exception("Failed to resolve playback via share token")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Playback backend unavailable"
        ) from exc
