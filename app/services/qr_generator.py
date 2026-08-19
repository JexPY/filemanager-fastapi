import base64
import io

import pyvips
import segno
import segno.helpers

from app.config import settings
from app.services.image_vips import sniff_format

# segno has no built-in logo/overlay helper (despite some docs/blog posts
# suggesting `segno.helpers.make_image_overlay` -- no such function exists in
# the installed segno release). Logo embedding here is done by hand: render
# the QR to a raster PNG via pyvips (or composite in SVG), with a resized logo
# (backed by a quiet-zone patch) onto its center.

# Cap the logo at this fraction of the QR's shorter side. Error correction
# level "H" recovers ~30% of modules; staying well under that -- plus the
# solid quiet-zone backing below -- keeps the code reliably scannable.
_LOGO_MAX_FRACTION = 0.22


class InvalidLogoError(ValueError):
    """Raised when the supplied logo bytes are invalid, unsupported, or oversized."""


def _logo_backing_geometry(num_modules: int, scale: int) -> tuple[int, int, int, int]:
    """The logo's centered backing square, in module units and pixels:
    (target_modules, backing_px, x_px, y_px).

    The single source of this formula -- previously computed independently in
    three places (the pre-calc in generate_qr_image, the PNG raster overlay,
    and the SVG overlay), which meant a future tweak applied to only one copy
    would desync the logo's actual size/placement between output formats.
    """
    target_modules = max(3, int(num_modules * _LOGO_MAX_FRACTION))
    if target_modules % 2 == 0:
        target_modules += 1
    backing_px = target_modules * scale
    start_module = (num_modules - target_modules) // 2
    x_px = start_module * scale
    y_px = start_module * scale
    return target_modules, backing_px, x_px, y_px


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


def _process_logo_thumbnail(
    logo_bytes: bytes, max_dim: int, light: str = "#ffffff"
) -> tuple[pyvips.Image, bytes]:
    """Validate and thumbnail logo bytes using shrink-on-load for minimal memory
    overhead and sub-10ms processing."""
    if sniff_format(logo_bytes) is None:
        raise InvalidLogoError("Unsupported logo image format")

    try:
        header = pyvips.Image.new_from_buffer(logo_bytes, "")
        if header.width * header.height > settings.MAX_IMAGE_PIXELS:
            raise InvalidLogoError("Logo exceeds maximum pixel limit")
    except pyvips.Error as exc:
        raise InvalidLogoError("Invalid logo image") from exc

    try:
        thumb = pyvips.Image.thumbnail_buffer(logo_bytes, max_dim, height=max_dim, crop="centre")
    except pyvips.Error as exc:
        raise InvalidLogoError("Invalid logo image") from exc

    bg_rgb = _hex_to_rgb(light)

    # Flatten any alpha channel onto background color and normalize to 3-band sRGB uchar
    if thumb.hasalpha() or thumb.bands in (2, 4):
        thumb = thumb.flatten(background=bg_rgb)
    thumb = thumb.colourspace("srgb").cast("uchar")
    if thumb.bands > 3:
        thumb = thumb[0:3]

    thumb_png = thumb.write_to_buffer(".png")
    return thumb, thumb_png


def _overlay_logo_png(
    qr_image: pyvips.Image,
    logo_thumb: pyvips.Image,
    scale: int = 10,
    symbol_size: tuple[int | float, int | float] | None = None,
    light: str = "#ffffff",
) -> pyvips.Image:
    bg_rgb = _hex_to_rgb(light)
    qr_w = qr_image.width
    num_modules = int(symbol_size[0]) if symbol_size else qr_w // scale

    _, backing_px, x_px, y_px = _logo_backing_geometry(num_modules, scale)

    # Crisp solid background patch aligned precisely to module boundaries (no anti-aliasing)
    backing = pyvips.Image.black(backing_px, backing_px, bands=3) + bg_rgb
    backing = backing.cast("uchar").colourspace("srgb")

    pad_x = (backing_px - logo_thumb.width) // 2
    pad_y = (backing_px - logo_thumb.height) // 2

    backing = backing.insert(logo_thumb, pad_x, pad_y)

    if qr_image.hasalpha():
        qr_image = qr_image.flatten(background=bg_rgb).colourspace("srgb").cast("uchar")

    return qr_image.insert(backing, x_px, y_px)


def _generate_svg_with_logo(
    qr: segno.QRCode,
    scale: int,
    logo_thumb: pyvips.Image,
    logo_png: bytes,
    dark: str = "#000000",
    light: str = "#ffffff",
) -> bytes:
    """Embeds a resized logo with a solid quiet-zone backing rect inside vector SVG."""
    num_modules = int(qr.symbol_size()[0])
    _, backing_px, x_px, y_px = _logo_backing_geometry(num_modules, scale)

    pad_x = (backing_px - logo_thumb.width) // 2
    pad_y = (backing_px - logo_thumb.height) // 2
    logo_x = x_px + pad_x
    logo_y = y_px + pad_y

    b64_logo = base64.b64encode(logo_png).decode("ascii")

    out = io.BytesIO()
    qr.save(out, kind="svg", scale=scale, dark=dark, light=light)
    svg_text = out.getvalue().decode("utf-8")

    overlay_xml = (
        f'<rect x="{x_px}" y="{y_px}" width="{backing_px}" height="{backing_px}" fill="{light}"/>'
        f'<image x="{logo_x}" y="{logo_y}" width="{logo_thumb.width}" height="{logo_thumb.height}" '
        f'href="data:image/png;base64,{b64_logo}"/>'
    )
    svg_text = svg_text.replace("</svg>", f"{overlay_xml}</svg>")
    return svg_text.encode("utf-8")


def generate_qr_image(
    content: str,
    scale: int = 10,
    dark: str = "#000000",
    light: str = "#ffffff",
    logo_bytes: bytes | None = None,
    output_format: str = "png",
) -> bytes:
    fmt = output_format.lower().strip()
    if fmt not in ("png", "svg"):
        # Every HTTP route already rejects a bad format via qr.py's own
        # _validate_format before this function is ever called, so this is
        # unreachable through the API -- it only guards a direct/programmatic
        # caller (tests, future non-HTTP use). Message kept in sync with
        # _validate_format's wording so the two don't drift independently.
        raise ValueError("Invalid format. Supported formats: png, svg")

    error_level = "h" if logo_bytes is not None else None
    qr = segno.make(content, error=error_level)
    if logo_bytes is not None and isinstance(qr.version, int) and qr.version < 3:
        qr = segno.make(content, error=error_level, version=3)

    # Fast path for standard QR codes without logo: direct serialization via
    # segno's own writer, skipping the pyvips SVG->raster round trip entirely.
    # For `fmt == "png"` this means the pixel format is no longer guaranteed
    # 3-band sRGB uchar the way _render_qr_image below always produces (segno
    # can emit a 1-band grayscale/paletted PNG for a plain two-color code) --
    # deliberately not normalized here, since doing so would decode+recompose
    # the image and defeat the entire point of this fast path. Nothing in this
    # API's contract (a raw PNG response, no declared pixel-format guarantee)
    # depends on band count; every PNG produced either way is valid and
    # decodes correctly.
    if logo_bytes is None:
        out = io.BytesIO()
        qr.save(out, kind=fmt, scale=scale, dark=dark, light=light)
        return out.getvalue()

    # Logo overlay path
    num_modules = int(qr.symbol_size()[0])
    _, backing_px, _, _ = _logo_backing_geometry(num_modules, scale)
    logo_max_dim = max(1, backing_px - 2 * scale)
    logo_thumb, logo_png = _process_logo_thumbnail(logo_bytes, logo_max_dim, light=light)

    if fmt == "svg":
        return _generate_svg_with_logo(qr, scale, logo_thumb, logo_png, dark=dark, light=light)

    image = _render_qr_image(qr, scale, dark, light)
    image = _overlay_logo_png(
        image, logo_thumb, scale=scale, symbol_size=qr.symbol_size(), light=light
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
    output_format: str = "png",
) -> bytes:
    content = segno.helpers.make_vcard_data(
        name=name,
        displayname=displayname if displayname is not None else name,
        email=email,
        phone=phone,
        url=url,
    )
    return generate_qr_image(
        content, scale=scale, logo_bytes=logo_bytes, output_format=output_format
    )


def generate_mecard_qr(
    name: str,
    email: str | None = None,
    phone: str | None = None,
    url: str | None = None,
    scale: int = 10,
    logo_bytes: bytes | None = None,
    output_format: str = "png",
) -> bytes:
    content = segno.helpers.make_mecard_data(
        name=name,
        email=email,
        phone=phone,
        url=url,
    )
    return generate_qr_image(
        content, scale=scale, logo_bytes=logo_bytes, output_format=output_format
    )


def generate_wifi_qr(
    ssid: str,
    password: str | None = None,
    security: str | None = None,
    hidden: bool = False,
    scale: int = 10,
    logo_bytes: bytes | None = None,
    output_format: str = "png",
) -> bytes:
    sec = None if security == "nopass" else security
    content = segno.helpers.make_wifi_data(
        ssid=ssid,
        password=password,
        security=sec,
        hidden=hidden,
    )
    return generate_qr_image(
        content, scale=scale, logo_bytes=logo_bytes, output_format=output_format
    )


def generate_geo_qr(
    lat: float,
    lng: float,
    scale: int = 10,
    logo_bytes: bytes | None = None,
    output_format: str = "png",
) -> bytes:
    content = segno.helpers.make_geo_data(lat=lat, lng=lng)
    return generate_qr_image(
        content, scale=scale, logo_bytes=logo_bytes, output_format=output_format
    )


def generate_epc_qr(
    name: str,
    iban: str,
    amount: float,
    text: str | None = None,
    scale: int = 10,
    logo_bytes: bytes | None = None,
    output_format: str = "png",
) -> bytes:
    content = segno.helpers._make_epc_qr_data(  # type: ignore[attr-defined]
        name=name,
        iban=iban,
        amount=amount,
        text=text,
    )
    return generate_qr_image(
        content, scale=scale, logo_bytes=logo_bytes, output_format=output_format
    )
