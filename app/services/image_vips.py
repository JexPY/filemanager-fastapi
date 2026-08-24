from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

import pyvips

from app.config import settings
from app.services.renditions import RENDITION_SPECS

logger = logging.getLogger(__name__)

# Bumped whenever a change to this module alters the bytes it produces for
# the same input. Folded into the upload dedup signature so a pipeline fix is
# not masked by a hit on a record produced by the previous pipeline.
# Sessions 3 and 4 bump this constant.
IMAGE_PIPELINE_VERSION = 3


@dataclass(frozen=True)
class _EncodeParams:
    q: int
    max_dim: int
    effort: int
    lossless: bool = False


@dataclass(frozen=True)
class ProcessedImage:
    buffer: bytes
    content_type: str
    width: int
    height: int
    renditions: dict[str, bytes]
    dominant_color: str | None = None
    blur_data_url: str | None = None


# Allow-list of accepted input formats, checked via magic bytes before pyvips
# ever touches the buffer. Deliberately excludes SVG: Dockerfile.api compiles
# libvips with librsvg support, so without this check any attacker-supplied
# SVG markup would be rasterized (a classic XXE/entity-expansion/SSRF vector),
# as would TIFF/HEIF/PDF and anything else libvips happens to load.
_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpeg": (b"\xff\xd8\xff",),
    "gif": (b"GIF87a", b"GIF89a"),
}


class ImageValidationError(Exception):
    """Any client-input image problem: unsupported format, oversized
    dimensions, or a corrupt/truncated file pyvips can't decode."""


def sniff_format(data: bytes) -> str | None:
    for fmt, signatures in _SIGNATURES.items():
        if any(data.startswith(sig) for sig in signatures):
            return fmt
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        major_brand = data[8:12]
        if major_brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "heic"
        # AVIF was previously entirely unrecognized here (even though
        # file_validation.py's _is_avif already accepts it for the generic
        # /upload/file route) -- a legitimate AVIF upload to /upload/image,
        # a QR logo, or a poster-generation frame was rejected as
        # "unsupported format" for no reason other than this list never
        # having been extended to it.
        if major_brand in {b"avif", b"avis"}:
            return "avif"
    return None


def _extract_placeholders(encoded: bytes) -> tuple[str, str]:
    """Return (dominant_color_hex, blur_data_url) for an already-encoded image.

    Deliberately derived from the *output* buffer rather than the full-resolution
    source, for two reasons that only show up on real photos:

    - pyvips recomputes a pipeline for every independent sink, so extracting from
      the source forced a second full-resolution decode + ICC transform. Measured
      on a 24.5MP iPhone photo: 591ms from the source vs 15.8ms here.
    - Running before the MAX_IMAGE_PIXELS guard meant a decompression bomb was
      fully decoded before being rejected (256ms vs 0.5ms for a 36MP input).

    Reading back the output -- already normalized to sRGB, already downscaled --
    avoids both, and is format-independent (shrink-on-load does not exist for
    PNG, so shrinking the *source* is slow for exactly the formats a bomb uses).
    At 16px on the long edge the lossy re-encode is immaterial.
    """
    tile = pyvips.Image.thumbnail_buffer(encoded, 16, height=16, size=pyvips.Size.DOWN)
    if tile.hasalpha():
        tile = tile.flatten(background=[255, 255, 255])

    if tile.bands >= 3:
        r = int(min(max(round(tile[0].avg()), 0), 255))
        g = int(min(max(round(tile[1].avg()), 0), 255))
        b = int(min(max(round(tile[2].avg()), 0), 255))
    elif tile.bands == 1:
        val = int(min(max(round(tile[0].avg()), 0), 255))
        r = g = b = val
    else:
        r = g = b = 0
    dominant_color = f"#{r:02x}{g:02x}{b:02x}"

    tile_buf = tile.write_to_buffer(".webp", Q=20, strip=True, effort=0)
    b64 = base64.b64encode(tile_buf).decode("ascii")
    blur_data_url = f"data:image/webp;base64,{b64}"

    return dominant_color, blur_data_url


def _generate_materialized_renditions(image: pyvips.Image, width: int) -> dict[str, bytes]:
    """Encode extra responsive width and thumbnail renditions in materialize mode.

    Renditions deliberately remain lossy regardless of the primary asset's
    optimization profile (e.g. `lossless`), because renditions are CDN
    accelerators where compact transfer size is the primary goal.
    """
    renditions: dict[str, bytes] = {}
    for spec in RENDITION_SPECS.values():
        if spec.crop:
            interesting = (
                pyvips.Interesting.ATTENTION
                if spec.crop_mode == "attention"
                else pyvips.Interesting.CENTRE
            )
            rend_image = image.thumbnail_image(spec.width, height=spec.height, crop=interesting)
        else:
            if width < spec.width:
                continue
            rend_image = image.thumbnail_image(
                spec.width,
                height=10_000_000,
                size=pyvips.Size.DOWN,
                crop=pyvips.Interesting.NONE,
            )
        renditions[spec.name] = rend_image.write_to_buffer(
            f".{spec.format}",
            Q=spec.quality,
            strip=True,
            effort=spec.effort,
            smart_subsample=True,
        )
    return renditions


def _get_optimization_params(optimization: str) -> _EncodeParams:
    """Return _EncodeParams for the given optimization profile."""
    if optimization == "size":
        return _EncodeParams(q=65, max_dim=1280, effort=4, lossless=False)
    if optimization == "quality":
        return _EncodeParams(q=95, max_dim=3840, effort=6, lossless=False)
    if optimization == "lossless":
        # libwebp hard limits dimensions to 16383px. Capping lossless at 4096px
        # ensures large scans never hit libwebp's hard ceiling while preserving
        # pixel fidelity for graphics, screenshots, and logos.
        # Q is a compression-effort level in lossless WebP rather than quality.
        return _EncodeParams(q=75, max_dim=4096, effort=4, lossless=True)
    return _EncodeParams(q=85, max_dim=1920, effort=4, lossless=False)


def validate_and_strip_image(
    file_data: bytes, optimization: str = "balanced", *, generate_renditions: bool = False
) -> ProcessedImage:
    """Load image using pyvips, normalize orientation and colour, strip metadata
    (EXIF/ICC), generate materialized thumbnail rendition if requested, and return
    the optimized bytes along with detected format, dimensions, and renditions buffers.

    `generate_renditions=False` skips rendition generation (returning an empty
    dict in its place) when the caller does not request a thumbnail.
    """
    if sniff_format(file_data) is None:
        raise ImageValidationError("Unsupported or unrecognized image format")

    try:
        image = pyvips.Image.new_from_buffer(file_data, "")
    except pyvips.Error as exc:
        raise ImageValidationError(f"Could not decode image: {exc}") from exc

    # 1. autorot before pixel check and resize because it can swap width/height,
    # and also clears the orientation tag from metadata.
    image = image.autorot()

    # 2. Convert embedded wide-gamut profile (Display P3, AdobeRGB) to sRGB before
    # strip=True drops the profile and before rendition generation.
    if image.get_typeof("icc-profile-data") != 0:
        try:
            image = image.icc_transform("srgb", embedded=True)
        except pyvips.Error as exc:
            # An unconvertible/corrupt profile must not fail the upload; leaving
            # the pixels untouched is the same behaviour as before this change.
            logger.warning("Failed to apply embedded ICC profile: %s", exc)

    # 3. Handle CMYK and greyscale inputs arriving with a non-sRGB interpretation.
    if image.interpretation != pyvips.Interpretation.SRGB:
        image = image.colourspace(pyvips.Interpretation.SRGB)

    width = image.width
    height = image.height
    if width * height > settings.MAX_IMAGE_PIXELS:
        raise ImageValidationError(
            f"Image dimensions {width}x{height} exceed the {settings.MAX_IMAGE_PIXELS}-pixel limit"
        )

    renditions: dict[str, bytes] = {}
    if generate_renditions and settings.IMAGE_RENDITION_MODE == "materialize":
        renditions = _generate_materialized_renditions(image, width)

    params = _get_optimization_params(optimization)

    if width > params.max_dim or height > params.max_dim:
        scale = min(params.max_dim / width, params.max_dim / height)
        image = image.resize(scale)
        width = image.width
        height = image.height

    if params.lossless:
        optimized_buffer = image.write_to_buffer(
            ".webp",
            Q=params.q,
            strip=True,
            effort=params.effort,
            lossless=True,
        )
    else:
        optimized_buffer = image.write_to_buffer(
            ".webp",
            Q=params.q,
            strip=True,
            effort=params.effort,
            smart_subsample=True,
        )

    # Placeholders come from the encoded output, not the source -- see
    # _extract_placeholders. This must stay after the encode; moving it earlier
    # reintroduces both a second full-resolution decode and a bypass of the
    # MAX_IMAGE_PIXELS guard above.
    dominant_color, blur_data_url = _extract_placeholders(optimized_buffer)

    return ProcessedImage(
        buffer=optimized_buffer,
        content_type="image/webp",
        width=width,
        height=height,
        renditions=renditions,
        dominant_color=dominant_color,
        blur_data_url=blur_data_url,
    )
