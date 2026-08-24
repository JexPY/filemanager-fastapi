import pytest
import pyvips

from app.config import settings
from app.services.image_vips import (
    ImageValidationError,
    ProcessedImage,
    sniff_format,
    validate_and_strip_image,
)
from tests.conftest import fixture_bytes


def test_accepts_valid_png() -> None:
    result = validate_and_strip_image(fixture_bytes("tiny.png"), generate_renditions=True)
    assert isinstance(result, ProcessedImage)
    assert result.content_type == "image/webp"
    assert result.width == 8
    assert result.height == 8
    assert result.buffer[:4] == b"RIFF"  # webp container signature
    assert "thumbnail" in result.renditions
    assert result.renditions["thumbnail"][:4] == b"RIFF"
    assert "medium" not in result.renditions


def test_generate_renditions_false_skips_rendition_work() -> None:
    """generate_renditions=False skips rendition generation entirely."""
    result = validate_and_strip_image(fixture_bytes("tiny.png"), generate_renditions=False)
    assert result.renditions == {}


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
    result = validate_and_strip_image(fixture_bytes("tiny.jpg"))
    assert result.content_type == "image/webp"


def test_accepts_valid_webp() -> None:
    result = validate_and_strip_image(fixture_bytes("tiny.webp"))
    assert result.content_type == "image/webp"


def test_accepts_valid_gif() -> None:
    result = validate_and_strip_image(fixture_bytes("tiny.gif"))
    assert result.content_type == "image/webp"


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


def test_orientation_is_applied() -> None:
    """exif_orientation_6.jpg has 40x20 stored pixels and orientation tag 6 (rotate 90 CW).

    autorot() must transpose/rotate the image to 20x40.
    """
    raw = fixture_bytes("exif_orientation_6.jpg")
    result = validate_and_strip_image(raw)
    assert result.width == 20
    assert result.height == 40


def test_orientation_propagates_to_renditions() -> None:
    """Renditions and main output are generated from the rotated image."""
    raw = fixture_bytes("exif_orientation_6.jpg")
    result = validate_and_strip_image(raw, generate_renditions=True)
    assert "thumbnail" in result.renditions

    # Verify thumbnail rendition was generated from rotated source (crop spec 300x300)
    thumb_img = pyvips.Image.new_from_buffer(result.renditions["thumbnail"], "")
    assert thumb_img.width == 300
    assert thumb_img.height == 300

    # Decoded main buffer dimensions match returned dimensions
    main_img = pyvips.Image.new_from_buffer(result.buffer, "")
    assert main_img.width == 20
    assert main_img.height == 40


def test_icc_transform_modifies_pixels() -> None:
    """Display P3 wide-gamut image is converted to sRGB color space before stripping.

    display_p3.jpg has saturated color [200, 100, 50] under Display P3 profile.
    Under ICC transform to sRGB, the pixel moves to [216, 93, 24].
    """
    raw = fixture_bytes("display_p3.jpg")
    result = validate_and_strip_image(raw)

    decoded = pyvips.Image.new_from_buffer(result.buffer, "")
    point = decoded.getpoint(20, 20)
    # Measured sRGB output for the Display P3 source (200, 100, 50). The
    # untransformed pixel decodes as (200, 100, 50), so the tolerance has to
    # stay below the smallest per-channel move for every assertion to be able
    # to fail: green only moves 7, which a tolerance of 8 could not detect.
    expected_r, expected_g, expected_b = 216.0, 93.0, 24.0
    tolerance = 4.0

    assert abs(point[0] - expected_r) <= tolerance
    assert abs(point[1] - expected_g) <= tolerance
    assert abs(point[2] - expected_b) <= tolerance


def test_image_without_icc_profile_is_untouched() -> None:
    """An image without an ICC profile passes through without ICC transform errors."""
    raw = fixture_bytes("tiny.png")
    result = validate_and_strip_image(raw)
    decoded = pyvips.Image.new_from_buffer(result.buffer, "")
    assert decoded.width == 8
    assert decoded.height == 8
    assert result.width == 8
    assert result.height == 8


def test_corrupt_icc_profile_does_not_fail_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unconvertible ICC profile is caught and the upload completes normally.

    The obvious way to write this -- attaching a garbage `icc-profile-data`
    blob and saving to JPEG -- does not work: jpegsave drops the invalid
    profile, so the reloaded image has no profile at all, the `get_typeof`
    guard is False, and the except branch is never reached. Instead, feed an
    image that genuinely carries a profile and force the transform to fail.
    `raising=False` is required because pyvips resolves operations
    dynamically, so `icc_transform` is not a real class attribute.
    """
    raw = fixture_bytes("display_p3.jpg")

    def _raise_icc_error(self, *args, **kwargs):
        raise pyvips.Error("simulated unconvertible ICC profile")

    monkeypatch.setattr(pyvips.Image, "icc_transform", _raise_icc_error, raising=False)

    result = validate_and_strip_image(raw)
    assert result.content_type == "image/webp"
    assert result.width == 40
    assert result.height == 40
