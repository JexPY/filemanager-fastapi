import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials

from app.routers.auth import _resolve_principal_optional, may_read_record, security
from app.routers.utils import resolve_playback
from app.services.metadata import (
    VISIBILITY_PUBLIC,
    MetadataError,
    UploadRecord,
    get_metadata_store,
)
from app.services.renditions import normalize_rendition_name
from app.services.storage import StorageError, get_storage, has_public_base_url

logger = logging.getLogger(__name__)
router = APIRouter()

_NOT_FOUND_DETAIL = "Not found"


def _fallback_filename(storage_key: str) -> str:
    """A sensible download name when the record has no original filename.

    The storage key's basename always carries the *correct* extension for what
    is actually stored (`<uuid>.webp`, `<uuid>_compressed.mp4`). The previous
    literal "video.mp4" default was harmless while this route was video-only;
    once it went kind-agnostic it started labelling every image as an mp4.
    """
    return storage_key.rsplit("/", 1)[-1]


def _resolve_rendition_target(record: UploadRecord, rendition: str | None) -> tuple[str, str, str]:
    """Given a record and optional rendition query, returns (storage_key, filename, content_type).

    Raises 400 on unsupported rendition names, and 404 when the requested
    rendition is not present on the record.
    """
    if rendition is None:
        return (
            record.storage_key,
            record.original_filename or _fallback_filename(record.storage_key),
            record.content_type or "application/octet-stream",
        )
    canonical_name = normalize_rendition_name(rendition)
    if canonical_name is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown or unsupported rendition {rendition!r}",
        )
    rend_key = record.renditions.get(canonical_name) if record.renditions else None
    if not rend_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rendition not found",
        )
    return (
        rend_key,
        _fallback_filename(rend_key),
        "image/webp",
    )


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
    rendition: Annotated[
        str | None,
        Query(
            description="Optional materialized rendition name (e.g. 'thumb', 'thumbnail', 't300')"
        ),
    ] = None,
):
    """The permanent, backend-agnostic URL -- the one clients embed.

    Kind-agnostic on purpose: an image, a video, and anything stored later go
    through the same route, the same auth, and the same per-backend byte path.
    Nothing here inspects `kind`.

    Visibility decides the auth *and* the URL form:

    * `public` -> tokenless. On an object store (s3/gcp) with a public/CDN base,
      302 to the stable `public_url(key)` (cacheable behind a CDN, app out of the
      read path; no per-request presigning, which would defeat that caching). On
      local, always the tokenless X-Accel path -- the media volume has no public
      URL to redirect to.
    * `private` -> the access ladder in `may_read_record`: a per-file `read:file`
      capability, or the owner. Anything else is 404 (not 403), matching the rest
      of the owner-scoping so existence never leaks. Then resolved per backend
      (local X-Accel / s3 presigned / gcs signed).

    The API only *issues* the redirect/header; it never proxies the bytes.
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

    target_key, target_filename, target_content_type = _resolve_rendition_target(record, rendition)

    if record.visibility == VISIBILITY_PUBLIC:
        # Object stores (s3/gcp) with a public/CDN base -> a stable, embeddable
        # 302 to public_url(key). NOT local: its media volume is served only via
        # nginx's internal X-Accel location, so a 302 to LOCAL_PUBLIC_BASE_URL/<key>
        # would dead-end -- local public videos fall through to the tokenless
        # X-Accel path below (resolve_playback), same as private ones.
        if has_public_base_url():
            backend = await get_storage()
            return RedirectResponse(
                url=backend.public_url(target_key), status_code=status.HTTP_302_FOUND
            )
    else:
        # Private: the owner, or a read capability bound to this exact record.
        # Anything else is a 404 (existence never leaks). Accepts the credential
        # via header OR ?token= -- a <video src>/<img src> cannot set headers,
        # which is precisely how a per-file grant reaches a browser.
        principal = _resolve_principal_optional(credentials, request)
        if not may_read_record(principal, record_owner=record.owner, record_id=record.id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    try:
        return await resolve_playback(
            target_key,
            filename=target_filename,
            media_type=target_content_type,
            private=record.visibility != VISIBILITY_PUBLIC,
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
async def play_shared_video(
    share_token: Annotated[str, Path(max_length=128)],
    rendition: Annotated[
        str | None,
        Query(
            description="Optional materialized rendition name (e.g. 'thumb', 'thumbnail', 't300')"
        ),
    ] = None,
):
    """Serve any shared record via an unlisted share token, regardless of kind or
    visibility. Does NOT require a bearer token -- the share token *is* the
    capability, and it is also the locator, so there is no id to owner-scope.
    404 on revoked/unknown."""
    store = await get_metadata_store()
    try:
        record = await store.get_by_share_token(share_token)
    except MetadataError as exc:
        logger.exception("Failed to load share token")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc

    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)

    target_key, target_filename, target_content_type = _resolve_rendition_target(record, rendition)

    try:
        return await resolve_playback(
            target_key,
            filename=target_filename,
            media_type=target_content_type,
            # A share link is a secret capability, so treat it like private
            # media: stream it rather than handing the holder a signed URL they
            # could pass on independently of the (revocable) share token.
            private=True,
        )
    except StorageError as exc:
        logger.exception("Failed to resolve playback via share token")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Playback backend unavailable"
        ) from exc
