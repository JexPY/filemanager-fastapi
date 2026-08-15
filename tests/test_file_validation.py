import os
import tempfile

import pytest

from app.services.file_validation import (
    FileValidationError,
    get_content_disposition_type,
    validate_file_content,
    validate_file_from_path,
)

PDF_SAMPLE = b"%PDF-1.4\n1 0 obj\n<<\n>>\nendobj\ntrailer\n<<\n>>\n%%EOF\n"
MP3_ID3_SAMPLE = b"ID3\x03\x00\x00\x00\x00\x00#\x00\x00" + b"\xff\xfb\x90d\x00\x00"
MP3_RAW_SAMPLE = b"\xff\xfb\x90d\x00\x00\x00\x00\x00\x00\x00\x00"
WAV_SAMPLE = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00"
OGG_SAMPLE = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
FLAC_SAMPLE = b'fLaC\x00\x00\x00"\x10\x00\x10\x00\x00\x00\x00\x00\x00\x00'
ZIP_SAMPLE = b"PK\x03\x04\x14\x00\x00\x00\x08\x00"
PNG_SAMPLE = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
JPEG_SAMPLE = b"\xff\xd8\xff\xe0\x00\x10JFIF"
GIF_SAMPLE = b"GIF89a\x01\x00\x01\x00"
WEBP_SAMPLE = b"RIFF\x1a\x00\x00\x00WEBPVP8 "
MP4_SAMPLE = b"\x00\x00\x00 ftypisom\x00\x00\x02\x00isomiso2mp41"
TEXT_SAMPLE = b"Hello, this is plain text content."
BINARY_SAMPLE = b"\x01\x02\x03\x04\x05\x06\x07\x08"


def test_validate_pdf() -> None:
    content_type = validate_file_content(PDF_SAMPLE, declared_content_type="application/pdf")
    assert content_type == "application/pdf"


def test_validate_pdf_sniffed_when_octet_stream() -> None:
    content_type = validate_file_content(
        PDF_SAMPLE, declared_content_type="application/octet-stream"
    )
    assert content_type == "application/pdf"


def test_validate_pdf_mismatch_rejected() -> None:
    with pytest.raises(FileValidationError, match="expected PDF"):
        validate_file_content(b"not a pdf content", declared_content_type="application/pdf")


def test_validate_audio_formats() -> None:
    assert validate_file_content(MP3_ID3_SAMPLE, "audio/mpeg") == "audio/mpeg"
    assert validate_file_content(MP3_RAW_SAMPLE, "audio/mpeg") == "audio/mpeg"
    assert validate_file_content(WAV_SAMPLE, "audio/wav") == "audio/wav"
    assert validate_file_content(OGG_SAMPLE, "audio/ogg") == "audio/ogg"
    assert validate_file_content(FLAC_SAMPLE, "audio/flac") == "audio/flac"


def test_validate_audio_mismatch_rejected() -> None:
    with pytest.raises(FileValidationError, match="expected MP3"):
        validate_file_content(b"garbage not mp3", "audio/mpeg")
    with pytest.raises(FileValidationError, match="expected WAV"):
        validate_file_content(MP3_ID3_SAMPLE, "audio/wav")


def test_validate_zip_archive() -> None:
    assert validate_file_content(ZIP_SAMPLE, "application/zip") == "application/zip"
    with pytest.raises(FileValidationError, match="expected ZIP"):
        validate_file_content(b"not a zip", "application/zip")


def test_validate_image_formats() -> None:
    assert validate_file_content(PNG_SAMPLE, "image/png") == "image/png"
    assert validate_file_content(JPEG_SAMPLE, "image/jpeg") == "image/jpeg"
    assert validate_file_content(GIF_SAMPLE, "image/gif") == "image/gif"
    assert validate_file_content(WEBP_SAMPLE, "image/webp") == "image/webp"


def test_validate_image_mismatch_rejected() -> None:
    with pytest.raises(FileValidationError, match="expected PNG"):
        validate_file_content(b"\x00\x01\x02\x03", "image/png")
    with pytest.raises(FileValidationError, match="expected JPEG"):
        validate_file_content(PNG_SAMPLE, "image/jpeg")


def test_validate_video_formats() -> None:
    assert validate_file_content(MP4_SAMPLE, "video/mp4") == "video/mp4"


def test_validate_video_mismatch_rejected() -> None:
    with pytest.raises(FileValidationError, match="expected MP4"):
        validate_file_content(b"\x00\x01\x02\x03", "video/mp4")


def test_validate_plain_text() -> None:
    assert validate_file_content(TEXT_SAMPLE, "text/plain") == "text/plain"


def test_validate_plain_text_strips_parameters() -> None:
    assert validate_file_content(TEXT_SAMPLE, "text/plain; charset=utf-8") == "text/plain"


def test_validate_plain_text_with_html_rejected() -> None:
    with pytest.raises(FileValidationError, match="suspicious HTML/XML tags"):
        validate_file_content(b"Hello <b>world</b>", "text/plain")


def test_validate_dangerous_content_types_rejected() -> None:
    dangerous_types = [
        "text/html",
        "image/svg+xml",
        "application/xhtml+xml",
        "text/javascript",
        "application/javascript",
        "application/x-msdownload",
        "application/x-executable",
        "application/x-anything",
    ]
    for dt in dangerous_types:
        with pytest.raises(FileValidationError, match="is not allowed"):
            validate_file_content(TEXT_SAMPLE, dt)


def test_validate_dangerous_payloads_rejected() -> None:
    # SVG markup
    with pytest.raises(FileValidationError, match="Unsupported or unsafe file type"):
        validate_file_content(b"<svg xmlns='http://www.w3.org/2000/svg'><circle/></svg>")

    with pytest.raises(FileValidationError, match="Unsupported or unsafe file type"):
        validate_file_content(b"<?xml version='1.0'?><svg>test</svg>")

    # HTML markup
    with pytest.raises(FileValidationError, match="Unsupported or unsafe file type"):
        validate_file_content(b"<!DOCTYPE html><html><body>test</body></html>")

    # Executable signatures
    with pytest.raises(FileValidationError, match="Unsupported or unsafe file type"):
        validate_file_content(b"MZ\x90\x00\x03\x00\x00\x00")  # Windows PE/DOS

    with pytest.raises(FileValidationError, match="Unsupported or unsafe file type"):
        validate_file_content(b"\x7fELF\x02\x01\x01\x00")  # Linux ELF

    with pytest.raises(FileValidationError, match="Unsupported or unsafe file type"):
        validate_file_content(b"\xca\xfe\xba\xbe\x00\x00\x00\x02")  # Mach-O


def test_security_reproduction_payloads_all_rejected() -> None:
    """Assert rejection for all exploit payloads from review."""
    pad = b"A" * 5000
    html = pad + b"<script>alert(document.domain)</script>"

    # 1. plain html
    with pytest.raises(FileValidationError):
        validate_file_content(b"<html><script>x</script>", declared_content_type="text/html")

    # 2. html with charset param
    with pytest.raises(FileValidationError):
        validate_file_content(html, declared_content_type="text/html; charset=utf-8")

    # 3. text/plain with late script
    with pytest.raises(FileValidationError):
        validate_file_content(html, declared_content_type="text/plain")

    # 4. svg with param
    with pytest.raises(FileValidationError):
        validate_file_content(
            pad + b"<svg onload=alert(1)>", declared_content_type="image/svg+xml; charset=utf-8"
        )

    # 5. arbitrary unlisted declared type
    with pytest.raises(FileValidationError, match="is not allowed"):
        validate_file_content(b"\x00\x01\x02\x03", declared_content_type="application/x-anything")

    # 6. declared video/mp4 with junk bytes
    with pytest.raises(FileValidationError, match="expected MP4"):
        validate_file_content(b"\x00\x01\x02\x03", declared_content_type="video/mp4")

    # 7. declared image/png with junk bytes
    with pytest.raises(FileValidationError, match="expected PNG"):
        validate_file_content(b"\x00\x01\x02\x03", declared_content_type="image/png")


def test_validate_opaque_binary() -> None:
    assert (
        validate_file_content(BINARY_SAMPLE, "application/octet-stream")
        == "application/octet-stream"
    )
    assert validate_file_content(BINARY_SAMPLE, filename="data.dat") == "application/octet-stream"


def test_content_disposition_type() -> None:
    assert get_content_disposition_type("application/pdf") == "inline"
    assert get_content_disposition_type("audio/mpeg") == "inline"
    assert get_content_disposition_type("audio/wav") == "inline"
    assert get_content_disposition_type("video/mp4") == "inline"
    assert get_content_disposition_type("image/png") == "inline"
    assert get_content_disposition_type("text/plain") == "inline"
    assert get_content_disposition_type("text/csv") == "inline"
    assert get_content_disposition_type("application/json") == "inline"
    # Strips parameters
    assert get_content_disposition_type("text/plain; charset=utf-8") == "inline"
    assert get_content_disposition_type("application/zip; charset=utf-8") == "attachment"

    assert get_content_disposition_type("application/zip") == "attachment"
    assert get_content_disposition_type("application/octet-stream") == "attachment"
    assert get_content_disposition_type("application/gzip") == "attachment"


def test_validate_file_from_path_late_script_past_prefix_bound_rejected() -> None:
    """A text file with markup located past the 8192-byte prefix bound must be rejected."""
    pad = b"A" * 9000
    html = pad + b"<script>alert(document.domain)</script>"

    with tempfile.NamedTemporaryFile(delete=False) as tf:
        tf.write(html)
        tf_path = tf.name

    try:
        # Buffer check on only first 8192 bytes would have seen only 'A's:
        assert (
            validate_file_content(html[:8192], declared_content_type="text/plain") == "text/plain"
        )

        # validate_file_from_path scans the full file in bounded chunks and detects late markup:
        with pytest.raises(FileValidationError):
            validate_file_from_path(tf_path, declared_content_type="text/plain")
    finally:
        if os.path.exists(tf_path):
            os.remove(tf_path)


def test_validate_file_from_path_chunk_boundary_split_script_rejected() -> None:
    """Script tag split across a 64KB chunk boundary must be detected and rejected."""
    # Place '<scr' at byte 65533 and 'ipt>alert(1)</script>' at byte 65537
    prefix = b"B" * 65533
    split_payload = prefix + b"<script>alert(1)</script>"

    with tempfile.NamedTemporaryFile(delete=False) as tf:
        tf.write(split_payload)
        tf_path = tf.name

    try:
        with pytest.raises(FileValidationError):
            validate_file_from_path(tf_path, declared_content_type="text/plain")
    finally:
        if os.path.exists(tf_path):
            os.remove(tf_path)


def test_validate_file_from_path_large_clean_text_accepted() -> None:
    """Valid plain text, CSV, and JSON files exceeding the prefix bound pass validation."""
    clean_text = b"Line of clean data without any markup.\n" * 500  # ~20 KB

    with tempfile.NamedTemporaryFile(delete=False) as tf:
        tf.write(clean_text)
        tf_path = tf.name

    try:
        assert validate_file_from_path(tf_path, declared_content_type="text/plain") == "text/plain"
        assert validate_file_from_path(tf_path, declared_content_type="text/csv") == "text/csv"
    finally:
        if os.path.exists(tf_path):
            os.remove(tf_path)
