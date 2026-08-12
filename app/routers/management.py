import contextlib
import logging
import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel

from app.routers.auth import verify_token
from app.schemas import FileListResponse, FileRecord, ShareLinkResponse
from app.services.metadata import KIND_VIDEO, MetadataError, get_metadata_store
from app.services.storage import StorageError, delete_file
from app.urls import public_url

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/files",
    tags=["Files"],
    summary="List files",
    response_model=FileListResponse,
    response_model_exclude_unset=True,
)
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
        total_count = await store.count(owner, kind=kind)
    except MetadataError as exc:
        logger.error("Failed to list uploads for %s: %s", owner, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    return {
        "files": [record.to_public() for record in records],
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
    }


@router.get(
    "/files/{file_id}",
    tags=["Files"],
    summary="Get file",
    response_model=FileRecord,
    response_model_exclude_unset=True,
)
async def get_file(
    file_id: Annotated[str, Path(max_length=64)], owner: str = Depends(verify_token)
):
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
    visibility: Literal["public", "private"]


@router.patch(
    "/files/{file_id}",
    tags=["Files"],
    summary="Set file visibility",
    response_model=FileRecord,
    response_model_exclude_unset=True,
)
async def set_file_visibility(
    file_id: Annotated[str, Path(max_length=64)],
    body: _VisibilityBody,
    owner: str = Depends(verify_token),
):
    """Set a video's playback visibility (`private` | `public`). Owner-scoped
    (404, not 403, for anything that isn't the caller's, so existence never leaks).
    Video-only -- images are served through imgproxy and have no visibility model
    here. `public` makes /files/{id}/download and any share link fetchable without
    a token; `private` restricts /download to the owner.
    """
    # A bad `visibility` value is rejected by the _VisibilityBody Literal before
    # reaching here (422), so no manual value check is needed.
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



@router.post(
    "/files/{file_id}/share",
    tags=["Sharing & Playback"],
    summary="Create share link",
    response_model=ShareLinkResponse,
)
async def create_share_link(
    file_id: Annotated[str, Path(max_length=64)], owner: str = Depends(verify_token)
):
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
        "share_url": public_url(f"/share/{token}"),
    }


@router.delete(
    "/files/{file_id}/share",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Sharing & Playback"],
    summary="Revoke share link",
)
async def revoke_share_link(
    file_id: Annotated[str, Path(max_length=64)], owner: str = Depends(verify_token)
):
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


@router.delete(
    "/files/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Files"],
    summary="Delete upload",
)
async def delete_upload(
    file_id: Annotated[str, Path(max_length=64)], owner: str = Depends(verify_token)
):
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
