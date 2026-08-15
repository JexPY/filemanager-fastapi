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


class FileValidationError(Exception):
    """Raised when file content or content-type fails validation."""


_ALLOWED_CONTENT_TYPES = frozenset(
    {
        # Documents & Data
        "application/pdf",
        "text/plain",
        "text/csv",
        "application/json",
        "application/zip",
        "application/gzip",
        "application/x-tar",
        "application/octet-stream",
        # Audio
        "audio/mpeg",
        "audio/wav",
        "audio/ogg",
        "audio/flac",
        "audio/aac",
        "audio/mp4",
        "audio/webm",
        # Video
        "video/mp4",
        "video/webm",
        "video/quicktime",
        "video/x-matroska",
        "video/ogg",
        # Images (safe raster formats only; SVG is explicitly excluded)
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
    }
)

_MIME_ALIASES: dict[str, str] = {
    "audio/mp3": "audio/mpeg",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "application/ogg": "audio/ogg",
    "audio/x-flac": "audio/flac",
    "audio/x-m4a": "audio/mp4",
    "application/x-zip-compressed": "application/zip",
    "application/x-gzip": "application/gzip",
    "application/tar": "application/x-tar",
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
}

_INLINE_MEDIA_TYPES = frozenset(
    {
        "application/pdf",
        "text/plain",
        "text/csv",
        "application/json",
    }
)


def _normalize_media_type(media_type: str | None) -> str:
    """Strip MIME parameters, whitespace, and normalize aliases."""
    if not media_type:
        return ""
    base = media_type.split(";", 1)[0].strip().lower()
    return _MIME_ALIASES.get(base, base)


def _sniff_magic_type(header: bytes) -> str | None:
    """Sniff standard safe formats from initial file header bytes."""
    if len(header) >= 4 and header.startswith(b"%PDF-"):
        return "application/pdf"

    # Audio signatures
    if len(header) >= 3 and header.startswith(b"ID3"):
        return "audio/mpeg"
    if (
        len(header) >= 2
        and header[0] == 0xFF
        and (header[1] & 0xE0) == 0xE0
        and (header[1] & 0x18) != 0x08
    ):
        return "audio/mpeg"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "audio/wav"
    if len(header) >= 4 and header.startswith(b"OggS"):
        return "audio/ogg"
    if len(header) >= 4 and header.startswith(b"fLaC"):
        return "audio/flac"
    if len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xF6) in (0xF0, 0xF1):
        return "audio/aac"

    # Archives / Compressed
    if len(header) >= 4 and header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "application/zip"
    if len(header) >= 2 and header.startswith(b"\x1f\x8b"):
        return "application/gzip"
    if len(header) >= 262 and header[257:262] == b"ustar":
        return "application/x-tar"

    # Images
    if len(header) >= 8 and header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(header) >= 3 and header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(header) >= 6 and header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"

    # ISO Base Media File Format (MP4 / M4A / AVIF / QuickTime)
    if len(header) >= 12 and header[4:8] == b"ftyp":
        major_brand = header[8:12]
        if major_brand in {b"M4A ", b"m4a "}:
            return "audio/mp4"
        if major_brand in {b"avif", b"avis"}:
            return "image/avif"
        if major_brand == b"qt  ":
            return "video/quicktime"
        return "video/mp4"

    # EBML header (WebM / MKV)
    if len(header) >= 4 and header.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"

    # QuickTime atom headers
    if len(header) >= 8 and header[4:8] in {b"moov", b"mdat", b"wide", b"free"}:
        return "video/quicktime"

    return None


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
    if lower.startswith(b"<?xml") and (b"<svg" in lower or b"<!doctype" in lower):
        return True

    return False


def _verify_magic_bytes_for_type(header: bytes, expected_type: str) -> None:
    """Verify that header bytes match the expected MIME type."""
    if expected_type == "application/pdf":
        if not (len(header) >= 4 and header.startswith(b"%PDF-")):
            raise FileValidationError(
                "File content does not match declared content-type (expected PDF)"
            )
        return

    if expected_type == "image/png":
        if not (len(header) >= 8 and header.startswith(b"\x89PNG\r\n\x1a\n")):
            raise FileValidationError(
                "File content does not match declared content-type (expected PNG)"
            )
        return

    if expected_type == "image/jpeg":
        if not (len(header) >= 3 and header.startswith(b"\xff\xd8\xff")):
            raise FileValidationError(
                "File content does not match declared content-type (expected JPEG)"
            )
        return

    if expected_type == "image/gif":
        if not (len(header) >= 6 and header.startswith((b"GIF87a", b"GIF89a"))):
            raise FileValidationError(
                "File content does not match declared content-type (expected GIF)"
            )
        return

    if expected_type == "image/webp":
        if not (len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP"):
            raise FileValidationError(
                "File content does not match declared content-type (expected WEBP)"
            )
        return

    if expected_type == "image/avif":
        is_avif = (
            len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12] in {b"avif", b"avis"}
        )
        if not is_avif:
            raise FileValidationError(
                "File content does not match declared content-type (expected AVIF)"
            )
        return

    if expected_type == "application/zip":
        if not (
            len(header) >= 4 and header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
        ):
            raise FileValidationError(
                "File content does not match declared content-type (expected ZIP)"
            )
        return

    if expected_type == "application/gzip":
        if not (len(header) >= 2 and header.startswith(b"\x1f\x8b")):
            raise FileValidationError(
                "File content does not match declared content-type (expected GZIP)"
            )
        return

    if expected_type == "application/x-tar":
        if not (len(header) >= 262 and header[257:262] == b"ustar"):
            raise FileValidationError(
                "File content does not match declared content-type (expected TAR)"
            )
        return

    # Audio formats
    if expected_type == "audio/mpeg":
        is_mp3 = (len(header) >= 3 and header.startswith(b"ID3")) or (
            len(header) >= 2
            and header[0] == 0xFF
            and (header[1] & 0xE0) == 0xE0
            and (header[1] & 0x18) != 0x08
        )
        if not is_mp3:
            raise FileValidationError(
                "File content does not match declared content-type (expected MP3)"
            )
        return

    if expected_type == "audio/wav":
        if not (len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WAVE"):
            raise FileValidationError(
                "File content does not match declared content-type (expected WAV)"
            )
        return

    if expected_type == "audio/ogg":
        if not (len(header) >= 4 and header.startswith(b"OggS")):
            raise FileValidationError(
                "File content does not match declared content-type (expected OGG)"
            )
        return

    if expected_type == "audio/flac":
        if not (len(header) >= 4 and header.startswith(b"fLaC")):
            raise FileValidationError(
                "File content does not match declared content-type (expected FLAC)"
            )
        return

    if expected_type == "audio/aac":
        if not (len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xF6) in (0xF0, 0xF1)):
            raise FileValidationError(
                "File content does not match declared content-type (expected AAC)"
            )
        return

    if expected_type == "audio/mp4":
        is_m4a = (
            len(header) >= 12
            and header[4:8] == b"ftyp"
            and header[8:12] in {b"M4A ", b"m4a ", b"mp41", b"mp42", b"isom", b"dash"}
        )
        if not is_m4a:
            raise FileValidationError(
                "File content does not match declared content-type (expected M4A/MP4)"
            )
        return

    if expected_type == "audio/webm":
        if not (len(header) >= 4 and header.startswith(b"\x1a\x45\xdf\xa3")):
            raise FileValidationError(
                "File content does not match declared content-type (expected WebM)"
            )
        return

    # Video formats
    if expected_type == "video/mp4":
        is_mp4 = len(header) >= 12 and header[4:8] == b"ftyp"
        if not is_mp4:
            raise FileValidationError(
                "File content does not match declared content-type (expected MP4)"
            )
        return

    if expected_type in {"video/webm", "video/x-matroska"}:
        if not (len(header) >= 4 and header.startswith(b"\x1a\x45\xdf\xa3")):
            name = "WebM" if expected_type == "video/webm" else "Matroska"
            raise FileValidationError(
                f"File content does not match declared content-type (expected {name})"
            )
        return

    if expected_type == "video/quicktime":
        is_qt = (len(header) >= 12 and header[4:8] == b"ftyp") or (
            len(header) >= 8 and header[4:8] in {b"moov", b"mdat", b"wide", b"free"}
        )
        if not is_qt:
            raise FileValidationError(
                "File content does not match declared content-type (expected QuickTime)"
            )
        return

    if expected_type == "video/ogg":
        if not (len(header) >= 4 and header.startswith(b"OggS")):
            raise FileValidationError(
                "File content does not match declared content-type (expected OGG)"
            )
        return


def validate_file_content(
    header: bytes,
    declared_content_type: str | None = None,
    filename: str | None = None,
) -> str:
    """Validate file content against security rules and declared content type.

    Returns the validated, normalized content type string or raises FileValidationError.
    """
    if _is_dangerous_content(header):
        raise FileValidationError("Unsupported or unsafe file type")

    declared = _normalize_media_type(declared_content_type)
    sniffed = _sniff_magic_type(header)

    # Empty or generic octet-stream: attempt sniffing, then filename guessing
    if not declared or declared == "application/octet-stream":
        if sniffed:
            return sniffed
        if filename:
            guessed, _ = mimetypes.guess_type(filename)
            if guessed:
                norm_guessed = _normalize_media_type(guessed)
                if norm_guessed in _ALLOWED_CONTENT_TYPES:
                    _verify_magic_bytes_for_type(header, norm_guessed)
                    return norm_guessed
        if not declared or declared == "application/octet-stream":
            return "application/octet-stream"

    # Declared type MUST be on the explicit allow-list
    if declared not in _ALLOWED_CONTENT_TYPES:
        raise FileValidationError(f"Content-Type {declared!r} is not allowed")

    # If the declared type has known magic bytes, the sniff MUST win and mismatch MUST reject
    _verify_magic_bytes_for_type(header, declared)

    # For text formats, ensure no HTML/XML/script injection anywhere in the header
    if declared in {"text/plain", "text/csv", "application/json"}:
        if re.search(rb"<[a-zA-Z/!?]", header):
            raise FileValidationError("Text file contains suspicious HTML/XML tags")

    return declared


def get_content_disposition_type(media_type: str) -> str:
    """Determine whether a media_type should be served inline or as an attachment."""
    normalized = _normalize_media_type(media_type)
    if normalized in _INLINE_MEDIA_TYPES or normalized.startswith(("audio/", "video/", "image/")):
        return "inline"
    return "attachment"
