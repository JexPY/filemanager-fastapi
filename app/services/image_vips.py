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

# Long edge of the throwaway thumbnail used by the lossless entropy probe.
_LOSSLESS_PROBE_SIDE = 512

# Output container format used for encoded buffers in this pipeline.
_WEBP_FORMAT = ".webp"


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


class LosslessUnsuitableError(ImageValidationError):
    """`optimization=lossless` was requested for content it is not for.

    A subclass so every existing `except ImageValidationError` still catches
    it, but distinguishable at the route so the caller can be told which
    parameter to change. The message the route emits is a fixed constant, not
    a stringified exception -- no internal detail reaches the client.
    """


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

    tile_buf = tile.write_to_buffer(_WEBP_FORMAT, Q=20, strip=True, effort=0)
    b64 = base64.b64encode(tile_buf).decode("ascii")
    blur_data_url = f"data:image/webp;base64,{b64}"

    return dominant_color, blur_data_url


def _lossless_probe_bytes_per_pixel(image: pyvips.Image) -> float:
    """Lossless-encode a 512px thumbnail and return its bytes-per-pixel.

    A cheap stand-in for content entropy, which is what actually drives
    lossless cost -- see the settings comment on
    LOSSLESS_MAX_PROBE_BYTES_PER_PIXEL for the measurements. `effort=0`
    because the probe only needs a size signal, not a small file.
    """
    small = image.thumbnail_image(
        _LOSSLESS_PROBE_SIDE,
        height=_LOSSLESS_PROBE_SIDE,
        size=pyvips.Size.DOWN,
        crop=pyvips.Interesting.NONE,
    )
    buf = small.write_to_buffer(_WEBP_FORMAT, lossless=True, Q=75, strip=True, effort=0)
    return len(buf) / max(small.width * small.height, 1)


def _reject_unsuitable_lossless(image: pyvips.Image) -> None:
    """Reject a lossless encode whose projected output would be oversized.

    `_lossless_probe_bytes_per_pixel` is a content signal that barely moves
    with image size (measured 1.265-1.297 across 0.25MP-12MP crops of the same
    photograph), so multiplying it by the real pixel count projects the actual
    output within about 1.5x -- enough to decide before paying for the encode.

    Projecting a size rather than thresholding the ratio matters: the ratio
    alone says "this is photographic", which is not by itself a problem. A 1MP
    photograph encodes losslessly in 389ms to 1.0MB, which is entirely
    affordable and should be allowed; a 12MP one takes 5.4s and is not. It also
    removes what would otherwise need an arbitrary small-image exemption --
    WebP's ~30-byte container overhead makes the ratio meaningless at tiny
    sizes (an 8x8 image scores ~0.5), but 0.5 * 64 pixels projects to 30 bytes,
    so small images pass on the projection without a special case.
    """
    projected = _lossless_probe_bytes_per_pixel(image) * image.width * image.height
    limit = settings.LOSSLESS_MAX_OUTPUT_BYTES
    if projected > limit:
        raise LosslessUnsuitableError(
            f"lossless would produce roughly {projected / 1e6:.1f}MB, over the "
            f"{limit / 1e6:.1f}MB limit (use optimization=quality)"
        )


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
        # Materialize once: the probe and the encode are two independent
        # pyvips sinks, and without this each would force its own full
        # decode (the same trap documented on _extract_placeholders). Bounded
        # by max_dim=4096, so at most ~50MB for this branch only.
        image = image.copy_memory()
        _reject_unsuitable_lossless(image)
        optimized_buffer = image.write_to_buffer(
            _WEBP_FORMAT,
            Q=params.q,
            strip=True,
            effort=params.effort,
            lossless=True,
        )
        if len(optimized_buffer) > settings.LOSSLESS_MAX_OUTPUT_BYTES:
            # Backstop for content the probe misjudged. The CPU is already
            # spent, but an unbounded object never reaches storage.
            raise LosslessUnsuitableError(
                f"lossless output {len(optimized_buffer)} bytes exceeds the "
                f"{settings.LOSSLESS_MAX_OUTPUT_BYTES}-byte limit"
            )
    else:
        optimized_buffer = image.write_to_buffer(
            _WEBP_FORMAT,
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
