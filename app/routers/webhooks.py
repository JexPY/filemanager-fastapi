import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status

from app.config import settings
from app.routers.auth import verify_token
from app.schemas import WebhookRedeliverResponse
from app.services.metadata import STATUS_PROCESSING, STATUS_READY, MetadataError, get_metadata_store
from app.tasks import deliver_webhook_task

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/files/{file_id}/redeliver",
    tags=["Webhooks"],
    summary="Redeliver webhook",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookRedeliverResponse,
)
async def redeliver_webhook(
    file_id: Annotated[str, Path(max_length=64)], owner: str = Depends(verify_token)
):
    """Re-enqueue the terminal-state webhook for one of the caller's uploads --
    the manual replay path for a dead-lettered (exhausted) delivery. Owner-scoped.
    Re-sends the current terminal event (`video.completed`/`video.failed`) with
    the same stable X-Webhook-Id, so a receiver that already saw it can dedupe.
    """
    store = await get_metadata_store()
    try:
        record = await store.get(file_id, owner)
    except MetadataError as exc:
        logger.error("Failed to load upload %s for redeliver: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Metadata store unavailable"
        ) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not settings.webhooks_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhooks are not enabled on this server",
        )
    if not record.callback_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No callback_url is configured for this upload",
        )
    if record.status == STATUS_PROCESSING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Video is still processing; no terminal event to deliver yet",
        )

    event = "video.completed" if record.status == STATUS_READY else "video.failed"
    try:
        task = await deliver_webhook_task.kiq(upload_id=file_id, event=event)
    except Exception as exc:
        logger.error("Failed to enqueue webhook redelivery for %s: %s", file_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Webhook redelivery temporarily unavailable",
        ) from exc

    return {"status": "accepted", "id": file_id, "event": event, "task_id": task.task_id}
