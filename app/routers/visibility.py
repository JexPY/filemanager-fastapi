"""Owner-controlled visibility (PATCH /files/{id}) and the storage-key
rotation it triggers when a record turns private.

Split out from ``management.py`` on purpose (see CLAUDE.md's "Single
Responsibility & Anti-Bloat" rule) -- rotation is the single largest,
most self-contained concern in that file (copy-to-fresh-key, rendition
rotation, old-object cleanup, the poster cascade, and the orphan-on-raise
cleanup all belong together), and management.py was well past the
~400-line threshold that calls for a split.
"""

import contextlib
import logging
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel

from app.routers.auth import verify_token
from app.schemas import FileRecord
from app.services.metadata import (
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    MetadataError,
    UploadRecord,
    get_metadata_store,
)
from app.services.renditions import derive_rendition_key
from app.services.storage import StorageError, StorageNotFound, copy_file, delete_file

logger = logging.getLogger(__name__)
router = APIRouter()

_STORE_UNAVAILABLE_DETAIL = "Metadata store unavailable"
_NOT_FOUND_DETAIL = "Not found"
_STORAGE_UNAVAILABLE_DETAIL = "Storage backend unavailable"


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


async def _cleanup_rotated_objects(
    rotated_key: str | None, rotated_renditions: dict[str, str] | None
) -> None:
    """Best-effort cleanup of objects `_rotate_key_if_going_private` already
    copied to fresh keys, for when the metadata update meant to re-point the
    row at them never lands (raised, or found the row gone) -- otherwise
    those copies are orphaned in storage forever, referenced by no row."""
    if rotated_key is None:
        return
    with contextlib.suppress(StorageError):
        await delete_file(rotated_key)
    if rotated_renditions:
        for rend_key in rotated_renditions.values():
            with contextlib.suppress(StorageError):
                await delete_file(rend_key)


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

    Applies to every kind. `public` makes the record accessible via direct CDN url or
    tokenless /files/{id}/download and carries thumbnail_url; `private` restricts
    /download to the owner or a per-file read grant, and withholds accelerator URLs.
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
        # The rotated copies (if any) already exist in storage, but the row
        # was never re-pointed at them -- clean them up rather than leaking.
        await _cleanup_rotated_objects(rotated_key, rotated_renditions)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_STORE_UNAVAILABLE_DETAIL
        ) from exc
    # updated is None only on a delete race between the load and the update;
    # treat it as gone. Same cleanup need as the raise above -- the row is
    # gone, so the rotated copies (if any) have nothing to point at.
    if updated is None:
        await _cleanup_rotated_objects(rotated_key, rotated_renditions)
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
    """Apply a visibility change (and any key rotation) to a video's poster.

    Best-effort by contract -- the caller wraps this whole call in
    `contextlib.suppress(StorageError, MetadataError, HTTPException)`, so a
    failure here must never fail the parent PATCH. That's exactly why the
    rotated-copy cleanup below can't be skipped: without it, a failure that
    the caller silently swallows would *also* silently orphan whatever
    `_rotate_key_if_going_private` already copied to storage.
    """
    store = await get_metadata_store()
    poster = await store.get(poster_id, owner)
    if poster is None or poster.visibility == visibility:
        return
    rotated_key, rotated_renditions = await _rotate_key_if_going_private(poster, visibility)
    try:
        updated = await store.set_visibility(
            poster_id, owner, visibility, rotated_key, renditions=rotated_renditions
        )
    except MetadataError:
        logger.warning(
            "Failed to cascade visibility to poster %s; cleaning up rotated copies", poster_id
        )
        await _cleanup_rotated_objects(rotated_key, rotated_renditions)
        raise
    if updated is None:
        # Poster deleted concurrently -- nothing left to point the rotated
        # copies (if any) at.
        await _cleanup_rotated_objects(rotated_key, rotated_renditions)
        return
    if rotated_key:
        with contextlib.suppress(StorageError):
            await delete_file(poster.storage_key)
        if poster.renditions:
            for old_rend_key in poster.renditions.values():
                with contextlib.suppress(StorageError):
                    await delete_file(old_rend_key)
