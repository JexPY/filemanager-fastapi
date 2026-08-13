import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Response, status

from app.routers.auth import verify_token
from app.schemas import PosterResponse
from app.services.metadata import KIND_VIDEO, STATUS_READY, MetadataError, get_metadata_store
from app.tasks import generate_poster_task

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/files/{file_id}/poster",
    tags=["Posters"],
    summary="Generate video poster",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PosterResponse,
    response_model_exclude_unset=True,
)
async def generate_poster(
    file_id: Annotated[str, Path(max_length=64)],
    response: Response,
    owner: Annotated[str, Depends(verify_token)],
    at_seconds: Annotated[float | None, Form(ge=0.0)] = None,
):
    """Generate a poster (thumbnail) image for one of the caller's *ready*
    videos, on request. Owner-scoped. Idempotent on the happy path: if a poster
    already exists it's returned inline (200) rather than regenerated. Otherwise
    a poster task is enqueued (202) and the client polls GET /files/{file_id}
    until `poster_upload_id` is set (then GET /files/{poster_id} for the image).

    `at_seconds` optionally picks the frame timestamp; the default is ~10% into
    the clip (avoiding a black lead-in frame). Two simultaneous requests can each
    generate a poster (best-effort, like the upload-idempotency paths); the last
    link wins and the loser is a harmless standalone image row.
    """
    store = await get_metadata_store()
    try:
        record = await store.get(file_id, owner)
    except MetadataError as exc:
        logger.exception("Failed to load upload for poster")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if record.kind != KIND_VIDEO:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Posters are only for videos"
        )
    if record.status != STATUS_READY:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Video is not ready")

    # Already has a live poster -> return it, don't regenerate. (If the linked
    # poster row was deleted out from under the video, fall through and remake.)
    if record.poster_upload_id is not None:
        existing = await store.get(record.poster_upload_id, owner)
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            return {"status": "ready", "video_id": file_id, "poster": existing.to_public()}

    try:
        task = await generate_poster_task.kiq(upload_id=file_id, at_seconds=at_seconds)
    except Exception as exc:
        logger.error("Failed to enqueue poster generation for %s: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Poster generation temporarily unavailable",
        ) from exc

    from app.urls import public_url

    return {
        "status": "accepted",
        "video_id": file_id,
        "task_id": task.task_id,
        "poll": public_url(f"/files/{file_id}"),
    }
