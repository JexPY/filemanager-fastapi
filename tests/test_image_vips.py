import base64
import re
import time

import pytest
import pyvips

from app.config import settings
from app.services import image_vips
from app.services.image_vips import (
    ImageValidationError,
    LosslessUnsuitableError,
    ProcessedImage,
    _extract_placeholders,
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


def test_placeholders_extracted_and_bounded() -> None:
    """Every ready image extracts dominant_color and blur_data_url within budget bounds."""
    raw = fixture_bytes("tiny.png")
    result = validate_and_strip_image(raw)

    # 1. Dominant colour matches 7-char hex string #rrggbb
    assert result.dominant_color is not None
    assert re.match(r"^#[0-9a-f]{6}$", result.dominant_color)

    # 2. blur_data_url starts with data URI scheme
    assert result.blur_data_url is not None
    assert result.blur_data_url.startswith("data:image/webp;base64,")

    # 3. Decoded payload is a valid WebP image with long edge <= 16px
    b64_payload = result.blur_data_url.removeprefix("data:image/webp;base64,")
    decoded_bytes = base64.b64decode(b64_payload)
    assert decoded_bytes[:4] == b"RIFF"
    assert decoded_bytes[8:12] == b"WEBP"

    tile_image = pyvips.Image.new_from_buffer(decoded_bytes, "")
    assert tile_image.width <= 16
    assert tile_image.height <= 16

    # 4. Payload budget tripwire: <= 1200 bytes
    assert len(result.blur_data_url) <= 1200


def test_dominant_color_accuracy_on_known_solid_color() -> None:
    """A known solid RGB colour produces approximately that hex colour."""
    # Synthesize a solid colour image: RGB (180, 50, 100) => #b43264
    img = pyvips.Image.black(32, 32, bands=3).copy(interpretation="srgb") + [180, 50, 100]
    png_bytes = img.write_to_buffer(".png")

    result = validate_and_strip_image(png_bytes)
    assert result.dominant_color is not None

    r = int(result.dominant_color[1:3], 16)
    g = int(result.dominant_color[3:5], 16)
    b = int(result.dominant_color[5:7], 16)

    # Separate atomic assertions per channel with tight tolerance
    assert abs(r - 180) <= 2
    assert abs(g - 50) <= 2
    assert abs(b - 100) <= 2


def test_placeholder_alpha_flattening() -> None:
    """Transparent PNG is flattened on white before computing dominant colour and blur tile."""
    # Transparent image: RGB (0, 0, 0) with alpha 0 => flattened on white (255, 255, 255) => #ffffff
    transparent_img = pyvips.Image.black(32, 32, bands=4)
    png_bytes = transparent_img.write_to_buffer(".png")

    result = validate_and_strip_image(png_bytes)
    assert result.dominant_color == "#ffffff"
    assert result.blur_data_url is not None
    assert result.blur_data_url.startswith("data:image/webp;base64,")


def test_placeholder_extraction_performance() -> None:
    """Extraction from the encoded output stays well inside the 15ms budget.

    Measured from real encoded bytes on purpose. An earlier version of this test
    called `.copy_memory()` on a pyvips image first, which pre-materialized the
    pixels and hid the decode that dominates the real cost -- it reported 1.6ms
    while a genuine 24.5MP photo took 591ms.
    """
    img = pyvips.Image.black(1920, 1280, bands=3).copy(interpretation="srgb") + [120, 140, 160]
    encoded = img.write_to_buffer(".webp", Q=85, strip=True, effort=4)

    _extract_placeholders(encoded)  # warm up libvips

    t0 = time.perf_counter()
    iterations = 10
    for _ in range(iterations):
        _extract_placeholders(encoded)
    duration_per_call_ms = ((time.perf_counter() - t0) / iterations) * 1000

    assert duration_per_call_ms < 15.0


def test_oversized_image_rejected_before_any_placeholder_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MAX_IMAGE_PIXELS guard must fire before any pixel work happens.

    pyvips is lazy, so whichever call forces evaluation first pays for a full
    decode. When placeholder extraction sat above this guard, a 36MP
    decompression bomb was fully decoded before being rejected -- 256ms versus
    0.5ms. Asserting on call ordering rather than elapsed time keeps this
    deterministic on a loaded CI box.
    """

    def _fail(*args: object, **kwargs: object) -> tuple[str, str]:
        raise AssertionError("placeholder extraction ran before the pixel-count guard")

    monkeypatch.setattr(settings, "MAX_IMAGE_PIXELS", 10)  # tiny.png is 8x8 = 64 pixels
    monkeypatch.setattr(image_vips, "_extract_placeholders", _fail)
    raw = fixture_bytes("tiny.png")

    with pytest.raises(ImageValidationError, match="exceed"):
        validate_and_strip_image(raw)


def test_optimization_lossless_produces_valid_webp() -> None:
    raw = fixture_bytes("tiny.png")
    balanced_result = validate_and_strip_image(raw, optimization="balanced")
    lossless_result = validate_and_strip_image(raw, optimization="lossless")

    assert lossless_result.content_type == "image/webp"
    assert lossless_result.buffer[:4] == b"RIFF"
    assert lossless_result.buffer[8:12] == b"WEBP"
    assert lossless_result.buffer != balanced_result.buffer


def test_lossless_roundtrip_pixel_identity() -> None:
    """Synthetic sharp-edged graphic fixture round-trips pixel-identically at lossless."""
    base = pyvips.Image.black(64, 64, bands=3).copy(interpretation="srgb") + [20, 40, 60]
    box = pyvips.Image.black(24, 24, bands=3).copy(interpretation="srgb") + [200, 100, 50]
    source_img = base.insert(box, 0, 0)
    raw_png = source_img.write_to_buffer(".png")

    result = validate_and_strip_image(raw_png, optimization="lossless")
    decoded = pyvips.Image.new_from_buffer(result.buffer, "")

    pt_box = decoded.getpoint(10, 10)
    assert pt_box[0] == 200.0
    assert pt_box[1] == 100.0
    assert pt_box[2] == 50.0

    pt_bg = decoded.getpoint(40, 40)
    assert pt_bg[0] == 20.0
    assert pt_bg[1] == 40.0
    assert pt_bg[2] == 60.0


def test_balanced_roundtrip_is_not_pixel_identical() -> None:
    """The same sharp-edged fixture at balanced introduces lossy compression drift."""
    base = pyvips.Image.black(64, 64, bands=3).copy(interpretation="srgb") + [20, 40, 60]
    box = pyvips.Image.black(24, 24, bands=3).copy(interpretation="srgb") + [200, 100, 50]
    source_img = base.insert(box, 0, 0)
    raw_png = source_img.write_to_buffer(".png")

    result = validate_and_strip_image(raw_png, optimization="balanced")
    decoded = pyvips.Image.new_from_buffer(result.buffer, "")

    pt_box = decoded.getpoint(10, 10)
    pt_bg = decoded.getpoint(40, 40)
    lossy_drift = (
        pt_box[0] != 200.0
        or pt_box[1] != 100.0
        or pt_box[2] != 50.0
        or pt_bg[0] != 20.0
        or pt_bg[1] != 40.0
        or pt_bg[2] != 60.0
    )
    assert lossy_drift is True


def test_attention_crop_differs_from_centre_crop() -> None:
    """Attention crop focuses on off-centre salient features rather than geometric centre."""
    dark_bg = pyvips.Image.black(800, 400, bands=3).copy(interpretation="srgb")
    bright_box = pyvips.Image.black(120, 120, bands=3).copy(interpretation="srgb") + [255, 255, 255]
    composite = dark_bg.insert(bright_box, 10, 10)
    raw_png = composite.write_to_buffer(".png")

    result = validate_and_strip_image(raw_png, generate_renditions=True)
    assert "thumbnail" in result.renditions

    thumb_attn = pyvips.Image.new_from_buffer(result.renditions["thumbnail"], "")
    assert thumb_attn.width == 300
    assert thumb_attn.height == 300

    img = pyvips.Image.new_from_buffer(raw_png, "")
    thumb_centre = img.thumbnail_image(300, height=300, crop=pyvips.Interesting.CENTRE)

    mean_attn = thumb_attn.avg()
    mean_centre = thumb_centre.avg()

    assert mean_attn != mean_centre
    assert mean_attn > mean_centre


def test_lossless_renditions_stay_lossy() -> None:
    """Materialized renditions remain lossy even when main asset uses lossless optimization."""
    raw = fixture_bytes("tiny.png")
    result = validate_and_strip_image(raw, optimization="lossless", generate_renditions=True)
    assert "thumbnail" in result.renditions
    assert result.renditions["thumbnail"][:4] == b"RIFF"

    img = pyvips.Image.new_from_buffer(raw, "")
    lossless_thumb = img.thumbnail_image(
        300, height=300, crop=pyvips.Interesting.ATTENTION
    ).write_to_buffer(".webp", Q=75, strip=True, effort=4, lossless=True)

    assert result.renditions["thumbnail"] != lossless_thumb


def _flat_graphic(width: int = 1100, height: int = 1000) -> bytes:
    """Screenshot-like content: flat fills and hard edges. What lossless is for."""
    im = (pyvips.Image.black(width, height, bands=3) + [246, 247, 249]).cast("uchar")
    bar = (pyvips.Image.black(int(width * 0.6), 18, bands=3) + [40, 44, 52]).cast("uchar")
    for i in range(8):
        im = im.insert(bar, 20, 20 + i * 40)
    return im.copy(interpretation="srgb").write_to_buffer(".png")


def _photographic(width: int = 1000, height: int = 1000) -> bytes:
    """High-entropy content standing in for a photograph."""
    noise = pyvips.Image.gaussnoise(width, height, mean=128, sigma=60)
    im = noise.bandjoin([noise.rot(pyvips.Angle.D180), noise * 0.8]).cast("uchar")
    return im.copy(interpretation="srgb").write_to_buffer(".png")


def test_lossless_accepts_flat_graphic_content() -> None:
    """The entropy probe must not reject the content lossless exists for."""
    result = validate_and_strip_image(_flat_graphic(), "lossless")
    assert result.content_type == "image/webp"
    assert result.buffer[:4] == b"RIFF"


def test_lossless_rejects_oversized_photographic_content() -> None:
    """A large photograph projects to an oversized object and is rejected.

    Measured: a 12.2MP photograph takes ~5.5s and produces ~6.1MB, while a
    *larger* 14.7MP flat-colour screenshot takes 262ms and produces 5KB. Cost
    tracks content, not pixel count, so the guard projects the output size from
    a cheap entropy probe rather than capping dimensions -- a dimension cap
    would block the intended use and permit the abusive one.
    """
    raw = _photographic(2600, 2600)  # ~6.8MP: projects well past the 8MB limit

    with pytest.raises(LosslessUnsuitableError, match="would produce"):
        validate_and_strip_image(raw, "lossless")


def test_lossless_allows_small_photographic_content() -> None:
    """Being photographic is not itself disqualifying -- only being expensive is.

    A 1MP photograph encodes losslessly in ~389ms to ~1.0MB. That is
    affordable, the caller asked for it explicitly, and rejecting it would make
    the guard do something its purpose does not justify.
    """
    result = validate_and_strip_image(_photographic(1000, 1000), "lossless")
    assert result.content_type == "image/webp"
    assert result.buffer[:4] == b"RIFF"


def test_tiny_image_passes_lossless_guard() -> None:
    """WebP's fixed container overhead makes bytes-per-pixel meaningless at
    tiny sizes (an 8x8 scores ~0.5, similar to a photograph). Projecting a
    size rather than thresholding the ratio handles this with no special
    case: 0.5 * 64 pixels is 30 bytes."""
    result = validate_and_strip_image(fixture_bytes("tiny.png"), "lossless")
    assert result.content_type == "image/webp"


def test_lossless_rejection_is_an_image_validation_error() -> None:
    """Subclassing matters: every existing `except ImageValidationError`
    (including the bulk-upload handler) must keep catching this."""
    raw = _photographic(2600, 2600)

    with pytest.raises(ImageValidationError):
        validate_and_strip_image(raw, "lossless")


def test_lossless_output_size_backstop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The projection is approximate, so the actual result is re-checked.

    Exercised by setting a limit the projection clears but the real encode
    does not.
    """
    raw = _flat_graphic(1400, 1200)
    monkeypatch.setattr(settings, "LOSSLESS_MAX_OUTPUT_BYTES", 200)

    with pytest.raises(LosslessUnsuitableError):
        validate_and_strip_image(raw, "lossless")
