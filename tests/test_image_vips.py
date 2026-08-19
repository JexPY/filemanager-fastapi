import pytest

from app.config import settings
from app.services.image_vips import ImageValidationError, sniff_format, validate_and_strip_image
from tests.conftest import fixture_bytes


def test_accepts_valid_png() -> None:
    data, content_type, width, height, renditions = validate_and_strip_image(
        fixture_bytes("tiny.png"), generate_renditions=True
    )
    assert content_type == "image/webp"
    assert width == 8
    assert height == 8
    assert data[:4] == b"RIFF"  # webp container signature
    assert "thumbnail" in renditions
    assert renditions["thumbnail"][:4] == b"RIFF"
    assert "medium" not in renditions


def test_generate_renditions_false_skips_rendition_work() -> None:
    """generate_renditions=False skips rendition generation entirely."""
    _, _, _, _, renditions = validate_and_strip_image(
        fixture_bytes("tiny.png"), generate_renditions=False
    )
    assert renditions == {}


def test_sniff_format_recognizes_avif_and_heic_brands() -> None:
    """Magic-byte-layer only (no fixture decodes these -- neither format has
    a real sample file, same as HEIC's pre-existing, likewise-untested
    status). AVIF was previously entirely unrecognized here even though
    file_validation.py's _is_avif already accepts it for /upload/file --  a
    legitimate AVIF upload to /upload/image, a QR logo, or a poster frame
    was rejected as "unsupported format" for no reason other than this list
    never having been extended to it."""
    avif_header = b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1"
    assert sniff_format(avif_header) == "avif"
    heic_header = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1"
    assert sniff_format(heic_header) == "heic"
    assert sniff_format(b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  ") is None


def test_accepts_valid_jpeg() -> None:
    _, content_type, _, _, _ = validate_and_strip_image(fixture_bytes("tiny.jpg"))
    assert content_type == "image/webp"


def test_accepts_valid_webp() -> None:
    validate_and_strip_image(fixture_bytes("tiny.webp"))


def test_accepts_valid_gif() -> None:
    validate_and_strip_image(fixture_bytes("tiny.gif"))


def test_rejects_svg() -> None:
    # SVG is the primary risk this allow-list closes: libvips is built with
    # librsvg support, so without this check attacker-supplied SVG markup
    # would be rasterized (XXE/entity-expansion/SSRF vector).
    raw = fixture_bytes("tiny.svg")
    with pytest.raises(ImageValidationError, match="Unsupported"):
        validate_and_strip_image(raw)


def test_rejects_non_image_bytes() -> None:
    raw = fixture_bytes("corrupt.bin")
    with pytest.raises(ImageValidationError, match="Unsupported"):
        validate_and_strip_image(raw)


def test_rejects_truncated_but_correctly_signed_file() -> None:
    # Passes the magic-byte sniff (valid PNG signature) but pyvips can't
    # actually decode it -- exercises the decode-failure path separately
    # from the format-rejection path above.
    raw = fixture_bytes("truncated.png")
    with pytest.raises(ImageValidationError, match="decode"):
        validate_and_strip_image(raw)


def test_rejects_oversized_pixel_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MAX_IMAGE_PIXELS", 10)  # tiny.png is 8x8 = 64 pixels
    raw = fixture_bytes("tiny.png")
    with pytest.raises(ImageValidationError, match="exceed"):
        validate_and_strip_image(raw)
