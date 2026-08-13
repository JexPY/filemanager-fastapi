import io

import pyvips
import segno

# segno has no built-in logo/overlay helper (despite some docs/blog posts
# suggesting `segno.helpers.make_image_overlay` -- no such function exists in
# the installed segno release). Logo embedding here is done by hand: render
# the QR to a raster PNG via pyvips, then composite a resized logo (backed by
# a `light`-colored quiet zone) onto its center.

# Cap the logo at this fraction of the QR's shorter side. Error correction
# level "H" recovers ~30% of modules; staying well under that -- plus the
# solid quiet-zone backing below -- keeps the code reliably scannable.
_LOGO_MAX_FRACTION = 0.22


class InvalidLogoError(ValueError):
    """Raised when the supplied logo bytes can't be decoded as an image."""


def _hex_to_rgb(hex_color: str) -> list[int]:
    value = hex_color.lstrip("#")
    return [int(value[i : i + 2], 16) for i in (0, 2, 4)]


def _render_qr_image(qr: segno.QRCode, scale: int, dark: str, light: str) -> pyvips.Image:
    out = io.BytesIO()
    qr.save(out, kind="svg", scale=scale, dark=dark, light=light)
    svg_data = out.getvalue()

    image = pyvips.Image.new_from_buffer(svg_data, "")
    # The SVG rasterizer may hand back an alpha channel even though the
    # background rect it drew is fully opaque; flatten it onto `light` so
    # every downstream step deals with a plain, opaque RGB image.
    if image.hasalpha():
        image = image.flatten(background=_hex_to_rgb(light))
    return image.colourspace("srgb").cast("uchar")


def _overlay_logo(qr_image: pyvips.Image, logo_bytes: bytes, light: str) -> pyvips.Image:
    try:
        logo = pyvips.Image.new_from_buffer(logo_bytes, "")
    except pyvips.Error as exc:
        raise InvalidLogoError("Invalid logo image") from exc

    # Flatten any transparency onto the QR's light color and normalize to
    # plain sRGB so band counts line up with the QR raster.
    if logo.hasalpha():
        logo = logo.flatten(background=_hex_to_rgb(light))
    logo = logo.colourspace("srgb").cast("uchar")

    qr_w, qr_h = qr_image.width, qr_image.height
    target = max(1, int(min(qr_w, qr_h) * _LOGO_MAX_FRACTION))
    logo = logo.thumbnail_image(target, height=target, crop="centre")

    # Solid quiet-zone padding behind the logo so the modules it covers read
    # as a clean "light" region instead of jagged, unscannable edges.
    pad = max(2, target // 16)
    backing_w = logo.width + pad * 2
    backing_h = logo.height + pad * 2
    backing = pyvips.Image.black(backing_w, backing_h, bands=3) + _hex_to_rgb(light)
    backing = backing.cast("uchar").colourspace("srgb")
    backing = backing.insert(logo, pad, pad)

    x = (qr_w - backing_w) // 2
    y = (qr_h - backing_h) // 2
    return qr_image.insert(backing, x, y)


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
    # A logo covers part of the QR, so force the highest error correction
    # level ("H", ~30% recovery) whenever one is embedded; otherwise keep the
    # previous default (segno picks the best level up to "L").
    qr = segno.make(content, error="h" if logo_bytes is not None else None)

    image = _render_qr_image(qr, scale, dark, light)
    if logo_bytes is not None:
        image = _overlay_logo(image, logo_bytes, light)

    return image.write_to_buffer(".png")
