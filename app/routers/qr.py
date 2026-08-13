import asyncio
import logging
import re

import pyvips
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status

from app.config import settings
from app.routers.auth import verify_token
from app.services.qr_generator import (
    InvalidLogoError,
    generate_epc_qr,
    generate_geo_qr,
    generate_mecard_qr,
    generate_qr_image,
    generate_vcard_qr,
    generate_wifi_qr,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
_IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")

_SCALE_DESC = "QR code pixel scale factor (1 to 20, default 10)"
_LOGO_DESC = "Optional logo image overlay."
_MEDIA_TYPE_PNG = "image/png"


async def _run_qr_generator(func, *args, **kwargs) -> bytes:
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except (InvalidLogoError, pyvips.Error) as exc:
        logger.warning("QR generation rejected logo: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid logo image"
        ) from exc
    except ValueError as exc:
        logger.warning("QR generation rejected input: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid QR content"
        ) from exc


@router.post(
    "/generate/qrcode",
    tags=["QR Codes"],
    summary="Generate QR code",
    description=(
        "Generate a high-resolution QR code PNG with optional centered logo overlay. "
        "Accepted logo image formats: PNG, JPEG, GIF, WebP, HEIC (SVG is rejected for security)."
    ),
    dependencies=[Depends(verify_token)],
)
async def generate_qrcode(
    content: str = Form(
        ...,
        max_length=settings.MAX_QR_CONTENT_LENGTH,
        description="Text or URL to encode in the QR code",
    ),
    scale: int = Form(10, ge=1, le=20, description=_SCALE_DESC),
    logo: UploadFile | None = File(
        None,
        description="Optional logo image overlay. Accepted formats: PNG, JPEG, GIF, WebP, HEIC.",
    ),
):
    logo_bytes = await logo.read() if logo is not None else None
    png_data = await _run_qr_generator(
        generate_qr_image, content, scale=scale, logo_bytes=logo_bytes
    )
    return Response(content=png_data, media_type=_MEDIA_TYPE_PNG)


@router.post(
    "/generate/qrcode/vcard",
    tags=["QR Codes"],
    summary="Generate vCard QR code",
    description="Generate a vCard (v3.0) contact card QR code with optional logo overlay.",
    dependencies=[Depends(verify_token)],
)
async def generate_qrcode_vcard(
    name: str = Form(..., description="Full name or 'FamilyName;GivenName'"),
    displayname: str | None = Form(None, description="Formatted display name"),
    phone: str | None = Form(None, description="Phone number"),
    email: str | None = Form(None, description="Email address"),
    url: str | None = Form(None, description="Website URL"),
    scale: int = Form(10, ge=1, le=20, description=_SCALE_DESC),
    logo: UploadFile | None = File(None, description=_LOGO_DESC),
):
    if not name.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name cannot be empty")
    if email and email.strip() and not _EMAIL_RE.match(email.strip()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid email address format"
        )

    logo_bytes = await logo.read() if logo is not None else None
    png_data = await _run_qr_generator(
        generate_vcard_qr,
        name=name.strip(),
        displayname=displayname.strip() if displayname else None,
        email=email.strip() if email else None,
        phone=phone.strip() if phone else None,
        url=url.strip() if url else None,
        scale=scale,
        logo_bytes=logo_bytes,
    )
    return Response(content=png_data, media_type=_MEDIA_TYPE_PNG)


@router.post(
    "/generate/qrcode/mecard",
    tags=["QR Codes"],
    summary="Generate MeCard QR code",
    description="Generate a MeCard compact contact QR code with optional logo overlay.",
    dependencies=[Depends(verify_token)],
)
async def generate_qrcode_mecard(
    name: str = Form(..., description="Full name or 'FamilyName,GivenName'"),
    phone: str | None = Form(None, description="Phone number"),
    email: str | None = Form(None, description="Email address"),
    url: str | None = Form(None, description="Website URL"),
    scale: int = Form(10, ge=1, le=20, description=_SCALE_DESC),
    logo: UploadFile | None = File(None, description=_LOGO_DESC),
):
    if not name.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name cannot be empty")
    if email and email.strip() and not _EMAIL_RE.match(email.strip()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid email address format"
        )

    logo_bytes = await logo.read() if logo is not None else None
    png_data = await _run_qr_generator(
        generate_mecard_qr,
        name=name.strip(),
        email=email.strip() if email else None,
        phone=phone.strip() if phone else None,
        url=url.strip() if url else None,
        scale=scale,
        logo_bytes=logo_bytes,
    )
    return Response(content=png_data, media_type=_MEDIA_TYPE_PNG)


@router.post(
    "/generate/qrcode/wifi",
    tags=["QR Codes"],
    summary="Generate Wi-Fi Network QR code",
    description="Generate a Wi-Fi network configuration QR code with optional logo overlay.",
    dependencies=[Depends(verify_token)],
)
async def generate_qrcode_wifi(
    ssid: str = Form(..., description="Wi-Fi network SSID"),
    password: str | None = Form(None, description="Wi-Fi passphrase"),
    security: str | None = Form("WPA", description="Security type: WPA, WEP, or nopass"),
    hidden: bool = Form(False, description="True if hidden network SSID"),
    scale: int = Form(10, ge=1, le=20, description=_SCALE_DESC),
    logo: UploadFile | None = File(None, description=_LOGO_DESC),
):
    if not ssid.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SSID cannot be empty")
    if security and security.strip() not in ("WPA", "WEP", "nopass", ""):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid security type. Use WPA, WEP, or nopass",
        )

    logo_bytes = await logo.read() if logo is not None else None
    png_data = await _run_qr_generator(
        generate_wifi_qr,
        ssid=ssid.strip(),
        password=password if password else None,
        security=security.strip() if security else None,
        hidden=hidden,
        scale=scale,
        logo_bytes=logo_bytes,
    )
    return Response(content=png_data, media_type=_MEDIA_TYPE_PNG)


@router.post(
    "/generate/qrcode/geo",
    tags=["QR Codes"],
    summary="Generate Geo Location QR code",
    description=(
        "Generate a Geo Location (latitude & longitude) QR code with optional logo overlay."
    ),
    dependencies=[Depends(verify_token)],
)
async def generate_qrcode_geo(
    lat: float = Form(..., ge=-90.0, le=90.0, description="Latitude coordinate (-90.0 to 90.0)"),
    lng: float = Form(
        ..., ge=-180.0, le=180.0, description="Longitude coordinate (-180.0 to 180.0)"
    ),
    scale: int = Form(10, ge=1, le=20, description=_SCALE_DESC),
    logo: UploadFile | None = File(None, description=_LOGO_DESC),
):
    logo_bytes = await logo.read() if logo is not None else None
    png_data = await _run_qr_generator(
        generate_geo_qr,
        lat=lat,
        lng=lng,
        scale=scale,
        logo_bytes=logo_bytes,
    )
    return Response(content=png_data, media_type=_MEDIA_TYPE_PNG)


@router.post(
    "/generate/qrcode/epc",
    tags=["QR Codes"],
    summary="Generate EPC Payment QR code",
    description="Generate a SEPA EPC credit transfer payment QR code with optional logo overlay.",
    dependencies=[Depends(verify_token)],
)
async def generate_qrcode_epc(
    name: str = Form(..., max_length=70, description="Beneficiary recipient name (max 70 chars)"),
    iban: str = Form(..., description="Recipient SEPA IBAN"),
    amount: float = Form(
        ..., gt=0.0, le=999999999.99, description="Transfer amount in Euros (> 0)"
    ),
    text: str | None = Form(
        None, max_length=140, description="Remittance / payment reference text"
    ),
    scale: int = Form(10, ge=1, le=20, description=_SCALE_DESC),
    logo: UploadFile | None = File(None, description=_LOGO_DESC),
):
    if not name.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Beneficiary name cannot be empty"
        )
    normalized_iban = iban.replace(" ", "").upper()
    if not _IBAN_RE.match(normalized_iban):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid SEPA IBAN format"
        )

    logo_bytes = await logo.read() if logo is not None else None
    png_data = await _run_qr_generator(
        generate_epc_qr,
        name=name.strip(),
        iban=normalized_iban,
        amount=amount,
        text=text.strip() if text else None,
        scale=scale,
        logo_bytes=logo_bytes,
    )
    return Response(content=png_data, media_type=_MEDIA_TYPE_PNG)
