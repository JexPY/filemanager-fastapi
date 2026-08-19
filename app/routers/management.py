import contextlib
import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status

from app.routers.auth import verify_token
from app.schemas import FileListResponse, FileRecord, ShareLinkResponse
from app.services.metadata import (
    KIND_VIDEO,
    MetadataError,
    UploadRecord,
    get_metadata_store,
)
from app.services.storage import StorageError, delete_file
from app.urls import public_url

logger = logging.getLogger(__name__)
router = APIRouter()

_STORE_UNAVAILABLE_DETAIL = "Metadata store unavailable"
_NOT_FOUND_DETAIL = "Not found"
_STORAGE_UNAVAILABLE_DETAIL = "Storage backend unavailable"


@router.get(
    "/files",
    tags=["Files"],
    summary="List files",
    response_model=FileListResponse,
    response_model_exclude_unset=True,
)
async def list_files(
    owner: Annotated[str, Depends(verify_token)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    kind: Annotated[str | None, Query(description="Filter by kind (e.g., 'image', 'video', 'file')")] = None,
    status_filter: Annotated[str | None, Query(alias="status", description="Filter by status (e.g., 'ready', 'processing', 'failed')")] = None,
    visibility: Annotated[str | None, Query(description="Filter by visibility ('public' or 'private')")] = None,
):
    """List the caller's uploads, newest first. Owner-scoped: a token only ever
    sees its own records."""
    store = await get_metadata_store()
    try:
        records = await store.list(
            owner, 
            kind=kind, 
            status=status_filter, 
            visibility=visibility, 
            limit=limit, 
            offset=offset
        )
        total_count = await store.count(owner, kind=kind, status=status_filter, visibility=visibility)
    except MetadataError as exc:
        logger.exception("Failed to list uploads")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_STORE_UNAVAILABLE_DETAIL
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
    summary="Get file metadata",
    response_model=FileRecord,
    response_model_exclude_unset=True,
)
async def get_file(
    file_id: Annotated[str, Path(max_length=64)],
    owner: Annotated[str, Depends(verify_token)],
):
    """Get metadata for one upload. Owner-scoped (404 for anything that isn't the
    caller's, so existence never leaks across tokens). Returns the public schema
    (no storage internals)."""
    store = await get_metadata_store()
    try:
        record = await store.get(file_id, owner)
    except MetadataError as exc:
        logger.exception("Failed to load upload")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_STORE_UNAVAILABLE_DETAIL
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
    return record.to_public()


@router.post(
    "/files/{file_id}/share",
    tags=["Sharing & Playback"],
    summary="Create share link",
    response_model=ShareLinkResponse,
)
async def create_share_link(
    file_id: Annotated[str, Path(max_length=64)],
    owner: Annotated[str, Depends(verify_token)],
):
    """Mint (or rotate) an unlisted, revocable share token for one of the caller's
    records, of any kind. A valid token serves it regardless of visibility via
    GET /share/{token}. This is the ONLY endpoint that returns the token + the
    shareable URL -- it's a secret capability and never appears in to_public()
    (listings/webhooks/GET /files/{id}). Calling again rotates the token, which
    revokes the previous link.
    """
    store = await get_metadata_store()
    try:
        record = await store.get(file_id, owner)
    except MetadataError as exc:
        logger.exception("Failed to load upload for share")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_STORE_UNAVAILABLE_DETAIL
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)

    token = secrets.token_urlsafe(32)
    try:
        updated = await store.set_share_token(file_id, owner, token)
    except MetadataError as exc:
        logger.exception("Failed to set share token")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_STORE_UNAVAILABLE_DETAIL
        ) from exc
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
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
    file_id: Annotated[str, Path(max_length=64)],
    owner: Annotated[str, Depends(verify_token)],
):
    """Revoke the caller's video share link (clears the token; the old URL now
    404s). Owner-scoped. Idempotent -- revoking when there's no token is a no-op
    204, but a non-owner / unknown id is still 404."""
    store = await get_metadata_store()
    try:
        record = await store.get(file_id, owner)
    except MetadataError as exc:
        logger.exception("Failed to load upload for share revoke")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_STORE_UNAVAILABLE_DETAIL
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)

    try:
        await store.clear_share_token(file_id, owner)
    except MetadataError as exc:
        logger.exception("Failed to clear share token")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_STORE_UNAVAILABLE_DETAIL
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _cascade_delete_poster(store, record: UploadRecord, owner: str) -> None:
    """Cascade: a video's poster is a derived object with no standalone meaning,
    so remove it with its parent. Best-effort -- a poster-cleanup failure must
    not fail the delete that already succeeded (it would just leave the poster
    image row/object, findable and deletable on its own)."""
    if record.kind != KIND_VIDEO or not record.poster_upload_id:
        return
    try:
        poster = await store.get(record.poster_upload_id, owner)
    except MetadataError:
        poster = None
    if poster is not None:
        with contextlib.suppress(StorageError):
            await delete_file(poster.storage_key)
        if poster.renditions:
            for rend_key in poster.renditions.values():
                with contextlib.suppress(StorageError):
                    await delete_file(rend_key)
        with contextlib.suppress(MetadataError):
            await store.delete(poster.id, owner)


@router.delete(
    "/files/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Files"],
    summary="Delete upload",
)
async def delete_upload(
    file_id: Annotated[str, Path(max_length=64)],
    owner: Annotated[str, Depends(verify_token)],
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
        logger.exception("Failed to load upload for delete")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_STORE_UNAVAILABLE_DETAIL
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)

    try:
        await delete_file(record.storage_key)
        if record.renditions:
            for rend_key in record.renditions.values():
                with contextlib.suppress(StorageError):
                    await delete_file(rend_key)
    except StorageError as exc:
        logger.exception("Failed to delete object")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_STORAGE_UNAVAILABLE_DETAIL
        ) from exc

    try:
        await store.delete(file_id, owner)
    except MetadataError as exc:
        logger.exception("Failed to delete record")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_STORE_UNAVAILABLE_DETAIL
        ) from exc

    await _cascade_delete_poster(store, record, owner)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
