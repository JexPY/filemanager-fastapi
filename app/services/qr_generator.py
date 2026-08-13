import io

import pyvips
import segno
import segno.helpers

from app.config import settings
from app.services.image_vips import sniff_format

# segno has no built-in logo/overlay helper (despite some docs/blog posts
# suggesting `segno.helpers.make_image_overlay` -- no such function exists in
# the installed segno release). Logo embedding here is done by hand: render
# the QR to a raster PNG via pyvips, then composite a resized logo (backed by
# a white quiet-zone patch) onto its center.

# Cap the logo at this fraction of the QR's shorter side. Error correction
# level "H" recovers ~30% of modules; staying well under that -- plus the
# solid quiet-zone backing below -- keeps the code reliably scannable.
_LOGO_MAX_FRACTION = 0.22


class InvalidLogoError(ValueError):
    """Raised when the supplied logo bytes are invalid, unsupported, or oversized."""


def _hex_to_rgb(hex_color: str) -> list[int]:
    value = hex_color.lstrip("#")
    return [int(value[i : i + 2], 16) for i in (0, 2, 4)]


def _render_qr_image(
    qr: segno.QRCode, scale: int, dark: str = "#000000", light: str = "#ffffff"
) -> pyvips.Image:
    out = io.BytesIO()
    qr.save(out, kind="svg", scale=scale, dark=dark, light=light)
    svg_data = out.getvalue()

    image = pyvips.Image.new_from_buffer(svg_data, "")
    if image.hasalpha():
        image = image.flatten(background=_hex_to_rgb(light))
    return image.colourspace("srgb").cast("uchar")


def _overlay_logo(
    qr_image: pyvips.Image,
    logo_bytes: bytes,
    scale: int = 10,
    symbol_size: tuple[int | float, int | float] | None = None,
    light: str = "#ffffff",
) -> pyvips.Image:
    if sniff_format(logo_bytes) is None:
        raise InvalidLogoError("Unsupported logo image format")

    try:
        logo = pyvips.Image.new_from_buffer(logo_bytes, "")
    except pyvips.Error as exc:
        raise InvalidLogoError("Invalid logo image") from exc

    if logo.width * logo.height > settings.MAX_IMAGE_PIXELS:
        raise InvalidLogoError("Logo exceeds maximum pixel limit")

    bg_rgb = _hex_to_rgb(light)

    # Flatten any alpha channel onto background color and normalize to 3-band sRGB uchar
    if logo.hasalpha() or logo.bands in (2, 4):
        logo = logo.flatten(background=bg_rgb)
    logo = logo.colourspace("srgb").cast("uchar")
    if logo.bands > 3:
        logo = logo[0:3]

    qr_w = qr_image.width
    num_modules = symbol_size[0] if symbol_size else qr_w // scale

    # Snap backing dimensions to whole module units centered on the grid
    target_modules = max(3, int(num_modules * _LOGO_MAX_FRACTION))
    if target_modules % 2 == 0:
        target_modules += 1

    backing_px = target_modules * scale
    start_module = (num_modules - target_modules) // 2
    x_px = start_module * scale
    y_px = start_module * scale

    # Crisp solid background patch aligned precisely to module boundaries (no anti-aliasing)
    backing = pyvips.Image.black(backing_px, backing_px, bands=3) + bg_rgb
    backing = backing.cast("uchar").colourspace("srgb")

    # Resize logo to fit inside backing with a quiet margin
    logo_max_dim = max(1, backing_px - 2 * scale)
    logo = logo.thumbnail_image(logo_max_dim, height=logo_max_dim, crop="centre")

    pad_x = (backing_px - logo.width) // 2
    pad_y = (backing_px - logo.height) // 2

    backing = backing.insert(logo, pad_x, pad_y)

    if qr_image.hasalpha():
        qr_image = qr_image.flatten(background=bg_rgb).colourspace("srgb").cast("uchar")

    return qr_image.insert(backing, x_px, y_px)


def generate_qr_image(
    content: str,
    scale: int = 10,
    dark: str = "#000000",
    light: str = "#ffffff",
    logo_bytes: bytes | None = None,
) -> bytes:
    # `content` length is bounded by the router (Form max_length=settings.
    # MAX_QR_CONTENT_LENGTH) before this is ever called.
    #
    # A logo covers part of the QR in the center. Short inputs (like "string")
    # produce a Version 1 QR code (21x21 modules), where a logo covers >30% of data
    # modules, exceeding Reed-Solomon Level H capacity (~30%).
    # When a logo is present, set a minimum QR version of 3 (29x29 modules)
    # so logo coverage stays ~3% of data modules, keeping the code compact and scannable.
    error_level = "h" if logo_bytes is not None else None
    qr = segno.make(content, error=error_level)
    if logo_bytes is not None and isinstance(qr.version, int) and qr.version < 3:
        qr = segno.make(content, error=error_level, version=3)

    image = _render_qr_image(qr, scale, dark, light)
    if logo_bytes is not None:
        image = _overlay_logo(
            image, logo_bytes, scale=scale, symbol_size=qr.symbol_size(), light=light
        )

    return image.write_to_buffer(".png")


def generate_vcard_qr(
    name: str,
    displayname: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    url: str | None = None,
    scale: int = 10,
    logo_bytes: bytes | None = None,
) -> bytes:
    content = segno.helpers.make_vcard_data(
        name=name,
        displayname=displayname if displayname is not None else name,
        email=email,
        phone=phone,
        url=url,
    )
    return generate_qr_image(content, scale=scale, logo_bytes=logo_bytes)


def generate_mecard_qr(
    name: str,
    email: str | None = None,
    phone: str | None = None,
    url: str | None = None,
    scale: int = 10,
    logo_bytes: bytes | None = None,
) -> bytes:
    content = segno.helpers.make_mecard_data(
        name=name,
        email=email,
        phone=phone,
        url=url,
    )
    return generate_qr_image(content, scale=scale, logo_bytes=logo_bytes)


def generate_wifi_qr(
    ssid: str,
    password: str | None = None,
    security: str | None = None,
    hidden: bool = False,
    scale: int = 10,
    logo_bytes: bytes | None = None,
) -> bytes:
    sec = None if security == "nopass" else security
    content = segno.helpers.make_wifi_data(
        ssid=ssid,
        password=password,
        security=sec,
        hidden=hidden,
    )
    return generate_qr_image(content, scale=scale, logo_bytes=logo_bytes)


def generate_geo_qr(
    lat: float,
    lng: float,
    scale: int = 10,
    logo_bytes: bytes | None = None,
) -> bytes:
    content = segno.helpers.make_geo_data(lat=lat, lng=lng)
    return generate_qr_image(content, scale=scale, logo_bytes=logo_bytes)


def generate_epc_qr(
    name: str,
    iban: str,
    amount: float,
    text: str | None = None,
    scale: int = 10,
    logo_bytes: bytes | None = None,
) -> bytes:
    content = segno.helpers._make_epc_qr_data(  # type: ignore[attr-defined]
        name=name,
        iban=iban,
        amount=amount,
        text=text,
    )
    return generate_qr_image(content, scale=scale, logo_bytes=logo_bytes)
