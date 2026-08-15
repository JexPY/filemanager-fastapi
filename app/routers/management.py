import contextlib
import logging
import secrets
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel

from app.routers.auth import verify_token
from app.schemas import FileListResponse, FileRecord, ShareLinkResponse
from app.services.metadata import (
    KIND_VIDEO,
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    MetadataError,
    UploadRecord,
    get_metadata_store,
)
from app.services.renditions import derive_rendition_key
from app.services.storage import StorageError, StorageNotFound, copy_file, delete_file
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
    kind: Annotated[str | None, Query()] = None,
):
    """List the caller's uploads, newest first. Owner-scoped: a token only ever
    sees its own records. `kind` optionally filters to 'image' or 'video'."""
    store = await get_metadata_store()
    try:
        records = await store.list(owner, kind=kind, limit=limit, offset=offset)
        total_count = await store.count(owner, kind=kind)
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


class _VisibilityBody(BaseModel):
    visibility: Literal["public", "private"]


async def _rotate_renditions(record: UploadRecord, new_key: str) -> dict[str, str] | None:
    if not record.renditions:
        return None
    new_renditions: dict[str, str] = {}
    for rend_name, old_rend_key in record.renditions.items():
        new_rend_key = derive_rendition_key(new_key, rend_name)
        try:
            await copy_file(old_rend_key, new_rend_key)
            new_renditions[rend_name] = new_rend_key
        except StorageNotFound:
            logger.warning(
                "Skipping rendition key rotation for %s (%s): object %s is already gone",
                record.id,
                rend_name,
                old_rend_key,
            )
        except StorageError as exc:
            logger.exception("Failed to rotate rendition %s for %s", rend_name, record.id)
            with contextlib.suppress(StorageError):
                await delete_file(new_key)
            for k in new_renditions.values():
                with contextlib.suppress(StorageError):
                    await delete_file(k)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=_STORAGE_UNAVAILABLE_DETAIL
            ) from exc
    return new_renditions


async def _rotate_key_if_going_private(
    record: UploadRecord, new_visibility: str
) -> tuple[str | None, dict[str, str] | None]:
    """Copy a record's object (and any renditions) to fresh keys when it turns
    private; else (None, None).

    Making a record private has to invalidate what is already *out there*, not
    just stop advertising it. While public it may have been fetched through a
    CDN and embedded in rendered HTML, and neither can be recalled. Rotating the
    key kills every one of those in a single step: the object URL changes, and
    because an imgproxy URL signs its source, every rendition URL changes with
    it. Marking the row private without rotating would leave the old bytes
    served from a warm cache indefinitely.

    Only public -> private rotates. The reverse direction has nothing cached to
    invalidate, so it would be a pointless copy.

    The old objects are deleted only after the row is re-pointed (by the caller),
    so a failure here leaves the record on its original key and is retryable.
    Returns (new_key, new_renditions), or (None, None) when no rotation is needed.
    """
    if new_visibility != VISIBILITY_PRIVATE or record.visibility != VISIBILITY_PUBLIC:
        return None, None

    prefix, _, name = record.storage_key.rpartition("/")
    suffix = name.partition(".")[2]
    new_key = f"{prefix}/{uuid.uuid4().hex}{'.' + suffix if suffix else ''}"
    try:
        await copy_file(record.storage_key, new_key)
    except StorageNotFound:
        # The object is already gone -- a reachable state, since DELETE removes
        # the object before the row. There is nothing to rotate and nothing
        # cached that we could invalidate anyway, so let the visibility change
        # proceed rather than trapping the owner on a half-deleted record.
        logger.warning(
            "Skipping key rotation for %s: object %s is already gone",
            record.id,
            record.storage_key,
        )
        return None, None
    except StorageError as exc:
        logger.exception("Failed to rotate storage key for %s", record.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_STORAGE_UNAVAILABLE_DETAIL
        ) from exc

    # Also rotate any materialized renditions
    new_renditions = await _rotate_renditions(record, new_key)
    return new_key, new_renditions


async def _delete_old_objects_after_rotation(record: UploadRecord) -> None:
    try:
        await delete_file(record.storage_key)
    except StorageError:
        logger.error(
            "Rotated %s to a private key but could not delete the old object %s; "
            "its public URL stays fetchable until that object is removed",
            record.id,
            record.storage_key,
        )
    if record.renditions:
        for old_rend_key in record.renditions.values():
            try:
                await delete_file(old_rend_key)
            except StorageError:
                logger.error(
                    "Rotated %s to a private key but could not delete old rendition %s",
                    record.id,
                    old_rend_key,
                )


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
    owner: Annotated[str, Depends(verify_token)],
):
    """Set a record's visibility (`private` | `public`). Owner-scoped (404, not
    403, for anything that isn't the caller's, so existence never leaks).

    Applies to every kind. `public` makes /files/{id}/download fetchable without
    a token and lets the record carry the accelerator URLs (direct_url,
    thumbnail_url); `private` restricts /download to the owner or a per-file
    read grant, and withholds every URL that would bypass the app.
    """
    # A bad `visibility` value is rejected by the _VisibilityBody Literal before
    # reaching here (422), so no manual value check is needed.
    store = await get_metadata_store()
    try:
        record = await store.get(file_id, owner)
    except MetadataError as exc:
        logger.exception("Failed to load upload for visibility")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_STORE_UNAVAILABLE_DETAIL
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)

    rotated_key, rotated_renditions = await _rotate_key_if_going_private(record, body.visibility)

    try:
        updated = await store.set_visibility(
            file_id, owner, body.visibility, rotated_key, renditions=rotated_renditions
        )
    except MetadataError as exc:
        logger.exception("Failed to set visibility")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_STORE_UNAVAILABLE_DETAIL
        ) from exc
    # updated is None only on a delete race between the load and the update;
    # treat it as gone.
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)

    if rotated_key is not None:
        await _delete_old_objects_after_rotation(record)

    # A video's poster is its own record with its own key and its own public
    # URLs, so leaving it public would keep a thumbnail of the now-private video
    # served from cache -- the same hole one level down.
    if record.poster_upload_id:
        with contextlib.suppress(StorageError, MetadataError, HTTPException):
            await _cascade_visibility_to_poster(record.poster_upload_id, owner, body.visibility)

    return updated.to_public()


async def _cascade_visibility_to_poster(poster_id: str, owner: str, visibility: str) -> None:
    """Apply a visibility change (and any key rotation) to a video's poster."""
    store = await get_metadata_store()
    poster = await store.get(poster_id, owner)
    if poster is None or poster.visibility == visibility:
        return
    rotated_key, rotated_renditions = await _rotate_key_if_going_private(poster, visibility)
    if (
        await store.set_visibility(
            poster_id, owner, visibility, rotated_key, renditions=rotated_renditions
        )
        and rotated_key
    ):
        with contextlib.suppress(StorageError):
            await delete_file(poster.storage_key)
        if poster.renditions:
            for old_rend_key in poster.renditions.values():
                with contextlib.suppress(StorageError):
                    await delete_file(old_rend_key)


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
