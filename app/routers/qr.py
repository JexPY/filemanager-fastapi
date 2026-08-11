import asyncio
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Response, status

from app.config import settings
from app.routers.auth import verify_token
from app.services.qr_generator import generate_qr_image

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/generate/qrcode",
    tags=["QR Codes"],
    summary="Generate QR code",
    dependencies=[Depends(verify_token)],
)
async def generate_qrcode(content: str = Form(..., max_length=settings.MAX_QR_CONTENT_LENGTH)):
    try:
        png_data = await asyncio.to_thread(generate_qr_image, content)
    except ValueError as exc:
        # segno raises ValueError (e.g. DataOverflowError) for content it
        # can't encode -- a client-input problem, not a server fault. The
        # detail is client-safe (segno's own capacity-limit messages don't
        # leak internals), but keep it generic and log the real exception.
        logger.warning("QR generation rejected input: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid QR content"
        ) from exc
    return Response(content=png_data, media_type="image/png")
