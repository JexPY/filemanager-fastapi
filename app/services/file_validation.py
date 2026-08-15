"""Magic-byte discipline and content-type validation for generic file ingest.

Provides:
- An explicit allow-list of safe media types.
- Magic-byte sniffing for formats with distinct magic bytes (PDF, audio, archives, images, video).
- Strict verification that declared Content-Type matches actual byte signatures (sniff wins).
- Stripping of MIME parameters (e.g. ; charset=utf-8) before validation and storage.
- Rejection of dangerous formats (SVG, HTML, JS, executables) and arbitrary unknown types.
- Content-Disposition resolution ('inline' for displayable formats, 'attachment' for archives).
"""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Callable


class FileValidationError(Exception):
    """Raised when file content or content-type fails validation."""


# Documents & Data MIME constants
MIME_PDF = "application/pdf"
MIME_TEXT_PLAIN = "text/plain"
MIME_TEXT_CSV = "text/csv"
MIME_JSON = "application/json"
MIME_ZIP = "application/zip"
MIME_GZIP = "application/gzip"
MIME_TAR = "application/x-tar"
MIME_OCTET_STREAM = "application/octet-stream"

# Audio MIME constants
MIME_AUDIO_MPEG = "audio/mpeg"
MIME_AUDIO_WAV = "audio/wav"
MIME_AUDIO_OGG = "audio/ogg"
MIME_AUDIO_FLAC = "audio/flac"
MIME_AUDIO_AAC = "audio/aac"
MIME_AUDIO_MP4 = "audio/mp4"
MIME_AUDIO_WEBM = "audio/webm"

# Video MIME constants
MIME_VIDEO_MP4 = "video/mp4"
MIME_VIDEO_WEBM = "video/webm"
MIME_VIDEO_QUICKTIME = "video/quicktime"
MIME_VIDEO_MATROSKA = "video/x-matroska"
MIME_VIDEO_OGG = "video/ogg"

# Images (safe raster formats only; SVG is explicitly excluded)
MIME_IMAGE_PNG = "image/png"
MIME_IMAGE_JPEG = "image/jpeg"
MIME_IMAGE_GIF = "image/gif"
MIME_IMAGE_WEBP = "image/webp"
MIME_IMAGE_AVIF = "image/avif"


_ALLOWED_CONTENT_TYPES = frozenset(
    {
        # Documents & Data
        MIME_PDF,
        MIME_TEXT_PLAIN,
        MIME_TEXT_CSV,
        MIME_JSON,
        MIME_ZIP,
        MIME_GZIP,
        MIME_TAR,
        MIME_OCTET_STREAM,
        # Audio
        MIME_AUDIO_MPEG,
        MIME_AUDIO_WAV,
        MIME_AUDIO_OGG,
        MIME_AUDIO_FLAC,
        MIME_AUDIO_AAC,
        MIME_AUDIO_MP4,
        MIME_AUDIO_WEBM,
        # Video
        MIME_VIDEO_MP4,
        MIME_VIDEO_WEBM,
        MIME_VIDEO_QUICKTIME,
        MIME_VIDEO_MATROSKA,
        MIME_VIDEO_OGG,
        # Images (safe raster formats only; SVG is explicitly excluded)
        MIME_IMAGE_PNG,
        MIME_IMAGE_JPEG,
        MIME_IMAGE_GIF,
        MIME_IMAGE_WEBP,
        MIME_IMAGE_AVIF,
    }
)

_MIME_ALIASES: dict[str, str] = {
    "audio/mp3": MIME_AUDIO_MPEG,
    "audio/x-wav": MIME_AUDIO_WAV,
    "audio/wave": MIME_AUDIO_WAV,
    "application/ogg": MIME_AUDIO_OGG,
    "audio/x-flac": MIME_AUDIO_FLAC,
    "audio/x-m4a": MIME_AUDIO_MP4,
    "application/x-zip-compressed": MIME_ZIP,
    "application/x-gzip": MIME_GZIP,
    "application/tar": MIME_TAR,
    "image/jpg": MIME_IMAGE_JPEG,
    "image/pjpeg": MIME_IMAGE_JPEG,
}

_INLINE_MEDIA_TYPES = frozenset(
    {
        MIME_PDF,
        MIME_TEXT_PLAIN,
        MIME_TEXT_CSV,
        MIME_JSON,
    }
)


def _normalize_media_type(media_type: str | None) -> str:
    """Strip MIME parameters, whitespace, and normalize aliases."""
    if not media_type:
        return ""
    base = media_type.split(";", 1)[0].strip().lower()
    return _MIME_ALIASES.get(base, base)


def _is_mp3(header: bytes) -> bool:
    if len(header) >= 3 and header.startswith(b"ID3"):
        return True
    return (
        len(header) >= 2
        and header[0] == 0xFF
        and (header[1] & 0xE0) == 0xE0
        and (header[1] & 0x18) != 0x08
    )


def _is_aac(header: bytes) -> bool:
    return len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xF6) in (0xF0, 0xF1)


def _is_m4a(header: bytes) -> bool:
    return (
        len(header) >= 12
        and header[4:8] == b"ftyp"
        and header[8:12] in {b"M4A ", b"m4a ", b"mp41", b"mp42", b"isom", b"dash"}
    )


def _is_avif(header: bytes) -> bool:
    return len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12] in {b"avif", b"avis"}


def _is_quicktime(header: bytes) -> bool:
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return True
    return len(header) >= 8 and header[4:8] in {b"moov", b"mdat", b"wide", b"free"}


def _sniff_audio(header: bytes) -> str | None:
    if _is_mp3(header):
        return MIME_AUDIO_MPEG
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return MIME_AUDIO_WAV
    if len(header) >= 4 and header.startswith(b"OggS"):
        return MIME_AUDIO_OGG
    if len(header) >= 4 and header.startswith(b"fLaC"):
        return MIME_AUDIO_FLAC
    if _is_aac(header):
        return MIME_AUDIO_AAC
    return None


def _sniff_archive(header: bytes) -> str | None:
    if len(header) >= 4 and header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return MIME_ZIP
    if len(header) >= 2 and header.startswith(b"\x1f\x8b"):
        return MIME_GZIP
    if len(header) >= 262 and header[257:262] == b"ustar":
        return MIME_TAR
    return None


def _sniff_image(header: bytes) -> str | None:
    if len(header) >= 8 and header.startswith(b"\x89PNG\r\n\x1a\n"):
        return MIME_IMAGE_PNG
    if len(header) >= 3 and header.startswith(b"\xff\xd8\xff"):
        return MIME_IMAGE_JPEG
    if len(header) >= 6 and header.startswith((b"GIF87a", b"GIF89a")):
        return MIME_IMAGE_GIF
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return MIME_IMAGE_WEBP
    return None


def _sniff_isobmff(header: bytes) -> str | None:
    if len(header) < 12 or header[4:8] != b"ftyp":
        return None
    major_brand = header[8:12]
    if major_brand in {b"M4A ", b"m4a "}:
        return MIME_AUDIO_MP4
    if major_brand in {b"avif", b"avis"}:
        return MIME_IMAGE_AVIF
    if major_brand == b"qt  ":
        return MIME_VIDEO_QUICKTIME
    return MIME_VIDEO_MP4


def _sniff_video(header: bytes) -> str | None:
    if len(header) >= 4 and header.startswith(b"\x1a\x45\xdf\xa3"):
        return MIME_VIDEO_WEBM
    if len(header) >= 8 and header[4:8] in {b"moov", b"mdat", b"wide", b"free"}:
        return MIME_VIDEO_QUICKTIME
    return None


def _sniff_magic_type(header: bytes) -> str | None:
    """Sniff standard safe formats from initial file header bytes."""
    if len(header) >= 4 and header.startswith(b"%PDF-"):
        return MIME_PDF
    return (
        _sniff_audio(header)
        or _sniff_archive(header)
        or _sniff_image(header)
        or _sniff_isobmff(header)
        or _sniff_video(header)
    )


def _is_dangerous_content(header: bytes) -> bool:
    """Detect HTML, SVG, script, or executable signatures across the header buffer."""
    # Check for executable headers
    if len(header) >= 2 and header.startswith(b"MZ"):
        return True  # DOS / Windows PE executable
    if len(header) >= 4 and header.startswith(b"\x7fELF"):
        return True  # Linux ELF executable
    if len(header) >= 4 and header.startswith(
        (b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce")
    ):
        return True  # Mach-O executable

    # Scan the entire header buffer (not sliced) for markup or script tags
    lower = header.lower()
    html_patterns = [
        b"<!doctype html",
        b"<html",
        b"<script",
        b"<svg",
        b'xmlns="http://www.w3.org/2000/svg"',
        b"xmlns='http://www.w3.org/2000/svg'",
        b"javascript:",
        b"onload=",
        b"onerror=",
    ]
    for pattern in html_patterns:
        if pattern in lower:
            return True

    # XML containing SVG or HTML doctype
    return bool(lower.startswith(b"<?xml") and (b"<svg" in lower or b"<!doctype" in lower))


_MAGIC_VALIDATORS: dict[str, tuple[Callable[[bytes], bool], str]] = {
    MIME_PDF: (lambda h: len(h) >= 4 and h.startswith(b"%PDF-"), "PDF"),
    MIME_IMAGE_PNG: (lambda h: len(h) >= 8 and h.startswith(b"\x89PNG\r\n\x1a\n"), "PNG"),
    MIME_IMAGE_JPEG: (lambda h: len(h) >= 3 and h.startswith(b"\xff\xd8\xff"), "JPEG"),
    MIME_IMAGE_GIF: (lambda h: len(h) >= 6 and h.startswith((b"GIF87a", b"GIF89a")), "GIF"),
    MIME_IMAGE_WEBP: (
        lambda h: len(h) >= 12 and h.startswith(b"RIFF") and h[8:12] == b"WEBP",
        "WEBP",
    ),
    MIME_IMAGE_AVIF: (_is_avif, "AVIF"),
    MIME_ZIP: (
        lambda h: len(h) >= 4 and h.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")),
        "ZIP",
    ),
    MIME_GZIP: (lambda h: len(h) >= 2 and h.startswith(b"\x1f\x8b"), "GZIP"),
    MIME_TAR: (lambda h: len(h) >= 262 and h[257:262] == b"ustar", "TAR"),
    MIME_AUDIO_MPEG: (_is_mp3, "MP3"),
    MIME_AUDIO_WAV: (
        lambda h: len(h) >= 12 and h.startswith(b"RIFF") and h[8:12] == b"WAVE",
        "WAV",
    ),
    MIME_AUDIO_OGG: (lambda h: len(h) >= 4 and h.startswith(b"OggS"), "OGG"),
    MIME_AUDIO_FLAC: (lambda h: len(h) >= 4 and h.startswith(b"fLaC"), "FLAC"),
    MIME_AUDIO_AAC: (_is_aac, "AAC"),
    MIME_AUDIO_MP4: (_is_m4a, "M4A/MP4"),
    MIME_AUDIO_WEBM: (lambda h: len(h) >= 4 and h.startswith(b"\x1a\x45\xdf\xa3"), "WebM"),
    MIME_VIDEO_MP4: (lambda h: len(h) >= 12 and h[4:8] == b"ftyp", "MP4"),
    MIME_VIDEO_WEBM: (lambda h: len(h) >= 4 and h.startswith(b"\x1a\x45\xdf\xa3"), "WebM"),
    MIME_VIDEO_MATROSKA: (
        lambda h: len(h) >= 4 and h.startswith(b"\x1a\x45\xdf\xa3"),
        "Matroska",
    ),
    MIME_VIDEO_QUICKTIME: (_is_quicktime, "QuickTime"),
    MIME_VIDEO_OGG: (lambda h: len(h) >= 4 and h.startswith(b"OggS"), "OGG"),
}


def _verify_magic_bytes_for_type(header: bytes, expected_type: str) -> None:
    """Verify that header bytes match the expected MIME type."""
    validator = _MAGIC_VALIDATORS.get(expected_type)
    if validator is None:
        return
    check_fn, format_name = validator
    if not check_fn(header):
        raise FileValidationError(
            f"File content does not match declared content-type (expected {format_name})"
        )


_TEXT_INLINE_TYPES = frozenset({MIME_TEXT_PLAIN, MIME_TEXT_CSV, MIME_JSON})
_SCAN_CHUNK_SIZE = 64 * 1024
_OVERLAP_SIZE = 256


def _scan_file_for_dangerous_text(file_path: str) -> None:
    """Stream-scan a text file in bounded O(1) memory to guarantee no HTML/XML
    or dangerous script markup exists anywhere in the file."""
    with open(file_path, "rb") as f:
        carry = b""
        while True:
            chunk = f.read(_SCAN_CHUNK_SIZE)
            if not chunk:
                break
            window = carry + chunk
            if _is_dangerous_content(window):
                raise FileValidationError("Unsupported or unsafe file type")
            if re.search(rb"<[a-zA-Z/!?]", window):
                raise FileValidationError("Text file contains suspicious HTML/XML tags")
            carry = chunk[-_OVERLAP_SIZE:] if len(chunk) >= _OVERLAP_SIZE else chunk


def _guess_content_type_from_filename(filename: str | None, header: bytes) -> str | None:
    if not filename:
        return None
    guessed, _ = mimetypes.guess_type(filename)
    if not guessed:
        return None
    norm_guessed = _normalize_media_type(guessed)
    if norm_guessed in _ALLOWED_CONTENT_TYPES:
        _verify_magic_bytes_for_type(header, norm_guessed)
        return norm_guessed
    return None


def validate_file_content(
    header: bytes,
    declared_content_type: str | None = None,
    filename: str | None = None,
) -> str:
    """Validate initial header sample or buffer against magic bytes and security rules.

    Checks magic-byte signatures, allow-listed MIME types, and scans the provided
    buffer for dangerous executable headers or markup.

    Note: When validating an entire uploaded file on disk, use `validate_file_from_path`
    which additionally performs a streaming full-file chunked scan for text formats
    served inline where markup could appear beyond the initial sample.
    """
    if _is_dangerous_content(header):
        raise FileValidationError("Unsupported or unsafe file type")

    declared = _normalize_media_type(declared_content_type)
    sniffed = _sniff_magic_type(header)

    # Empty or generic octet-stream: attempt sniffing, then filename guessing
    if not declared or declared == MIME_OCTET_STREAM:
        if sniffed:
            return sniffed
        guessed = _guess_content_type_from_filename(filename, header)
        if guessed:
            return guessed
        return MIME_OCTET_STREAM

    # Declared type MUST be on the explicit allow-list
    if declared not in _ALLOWED_CONTENT_TYPES:
        raise FileValidationError(f"Content-Type {declared!r} is not allowed")

    # If the declared type has known magic bytes, the sniff MUST win and mismatch MUST reject
    _verify_magic_bytes_for_type(header, declared)

    # For text formats, ensure no HTML/XML/script injection anywhere in the header buffer
    if declared in _TEXT_INLINE_TYPES and re.search(rb"<[a-zA-Z/!?]", header):
        raise FileValidationError("Text file contains suspicious HTML/XML tags")

    return declared


def validate_file_from_path(
    file_path: str,
    declared_content_type: str | None = None,
    filename: str | None = None,
) -> str:
    """Validate a file on disk against security rules and content-type allow-list.

    Reads an initial 8192-byte header to verify magic bytes and determine content type
    via `validate_file_content`. For text formats served inline, performs a bounded
    O(1) memory chunked scan across the entire file to ensure no HTML/XML markup,
    scripts, or executable signatures are hidden anywhere in the file.
    """
    with open(file_path, "rb") as f:
        header = f.read(8192)

    content_type = validate_file_content(
        header,
        declared_content_type=declared_content_type,
        filename=filename,
    )

    if content_type in _TEXT_INLINE_TYPES:
        _scan_file_for_dangerous_text(file_path)

    return content_type


def get_content_disposition_type(media_type: str) -> str:
    """Determine whether a media_type should be served inline or as an attachment."""
    normalized = _normalize_media_type(media_type)
    if normalized in _INLINE_MEDIA_TYPES or normalized.startswith(("audio/", "video/", "image/")):
        return "inline"
    return "attachment"
