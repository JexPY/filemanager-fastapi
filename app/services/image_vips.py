import pyvips

from app.config import settings

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
    if (
        len(data) >= 12
        and data[4:8] == b"ftyp"
        and data[8:12] in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}
    ):
        return "heic"
    return None


def validate_and_strip_image(
    file_data: bytes, optimization: str = "balanced"
) -> tuple[bytes, str, int, int]:
    """
    Load image using pyvips, strip metadata (EXIF), and return the optimized bytes
    along with the detected format and dimensions.
    """
    if sniff_format(file_data) is None:
        raise ImageValidationError("Unsupported or unrecognized image format")

    try:
        image = pyvips.Image.new_from_buffer(file_data, "")
    except pyvips.Error as exc:
        raise ImageValidationError(f"Could not decode image: {exc}") from exc

    width = image.width
    height = image.height
    # libvips is demand-driven: new_from_buffer reads dimensions from the
    # header without materializing pixel data, so this check runs *before*
    # the expensive full-resolution decode that write_to_buffer triggers --
    # exactly where a decompression-bomb guard needs to sit.
    if width * height > settings.MAX_IMAGE_PIXELS:
        raise ImageValidationError(
            f"Image dimensions {width}x{height} exceed the {settings.MAX_IMAGE_PIXELS}-pixel limit"
        )

    # Determine quality and max dimension based on optimization profile
    if optimization == "size":
        q_value = 65
        max_dim = 1280
    elif optimization == "quality":
        q_value = 95
        max_dim = 3840
    else:  # "balanced"
        q_value = 85
        max_dim = 1920

    if width > max_dim or height > max_dim:
        scale = min(max_dim / width, max_dim / height)
        image = image.resize(scale)
        width = image.width
        height = image.height

    # Write to optimized webp format. strip=True removes ALL metadata
    # (EXIF/GPS, ICC profile, XMP) on output in a single call.
    # effort=6 maximizes compression efficiency (smallest size for given Q).
    # smart_subsample=True improves color sharpness for high contrast edges.
    optimized_buffer = image.write_to_buffer(
        ".webp", Q=q_value, strip=True, effort=6, smart_subsample=True
    )
    return optimized_buffer, "image/webp", width, height
