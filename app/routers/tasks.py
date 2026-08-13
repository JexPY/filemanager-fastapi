import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status

from app.broker import broker
from app.routers.auth import verify_token
from app.schemas import TaskStatusResponse
from app.services.metadata import MetadataError, get_metadata_store

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/tasks/{task_id}",
    tags=["Tasks"],
    summary="Get task status",
    response_model=TaskStatusResponse,
    response_model_exclude_unset=True,
)
async def get_task_status(
    task_id: Annotated[str, Path(max_length=100)],
    owner: Annotated[str, Depends(verify_token)],
):
    # Owner-scoped via the uploads record: a task_id that isn't this owner's
    # (or never existed) is a 404, so no one can poll another owner's task and
    # existence never leaks across tenants. The record is also the durable proof
    # the task was issued -- it distinguishes "still running" (record exists,
    # result not ready -> pending) from "never existed" (no record -> 404),
    # which is_result_ready alone cannot.
    store = await get_metadata_store()
    try:
        record = await store.get_by_task_id(task_id, owner)
    except MetadataError as exc:
        logger.exception("Failed to resolve task")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown task id")

    # The database is our source of truth for completed states
    if record.status == "ready":
        return {
            "task_id": task_id,
            "status": "completed",
            "result": {"status": "success", "upload_id": record.id},
        }
    elif record.status == "failed":
        return {"task_id": task_id, "status": "failed", "error": "Processing failed"}

    is_ready = await broker.result_backend.is_result_ready(task_id)
    if not is_ready:
        return {"task_id": task_id, "status": "pending"}

    task_result = await broker.result_backend.get_result(task_id)
    if task_result.is_err:
        # The underlying exception (e.g. raw ffmpeg stderr, which can include
        # internal /tmp paths) is server-side detail only -- never echoed to
        # the caller, unlike the rest of this route which was already careful
        # about that (StorageError -> generic 502 above).
        logger.warning("Task execution failed")
        return {"task_id": task_id, "status": "failed", "error": "Video processing failed"}

    return {"task_id": task_id, "status": "completed", "result": task_result.return_value}
