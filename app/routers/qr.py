import asyncio
import logging
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status

from app.config import settings
from app.routers.auth import verify_token
from app.services.qr_generator import InvalidLogoError, generate_qr_image

logger = logging.getLogger(__name__)
router = APIRouter()

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@router.post(
    "/generate/qrcode",
    tags=["QR Codes"],
    summary="Generate QR code",
    dependencies=[Depends(verify_token)],
)
async def generate_qrcode(
    content: str = Form(..., max_length=settings.MAX_QR_CONTENT_LENGTH),
    dark: str = Form("#000000"),
    light: str = Form("#ffffff"),
    scale: int = Form(10, ge=1, le=20),
    logo: UploadFile | None = File(None),
):
    if not _HEX_COLOR_RE.match(dark) or not _HEX_COLOR_RE.match(light):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid color format, use #rrggbb",
        )

    logo_bytes = await logo.read() if logo is not None else None

    try:
        png_data = await asyncio.to_thread(
            generate_qr_image, content, scale, dark, light, logo_bytes
        )
    except InvalidLogoError as exc:
        logger.warning("QR generation rejected logo: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid logo image"
        ) from exc
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
