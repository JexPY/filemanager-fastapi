"""Shared helpers, canonical naming, and key derivation for materialized renditions."""

from __future__ import annotations

ALLOWED_RENDITION_NAMES = frozenset({"thumbnail", "thumb", "t300"})

_RENDITION_ALIASES: dict[str, str] = {
    "thumbnail": "thumbnail",
    "thumb": "thumbnail",
    "t300": "thumbnail",
}

_RENDITION_SUFFIXES: dict[str, str] = {
    "thumbnail": "t300",
    "thumb": "t300",
    "t300": "t300",
}


def normalize_rendition_name(name: str | None) -> str | None:
    """Normalize a user-supplied rendition name or alias to its canonical name.

    Returns None if the rendition name is unknown or unsupported.
    """
    if not name:
        return None
    cleaned = name.strip().lower()
    return _RENDITION_ALIASES.get(cleaned)


def derive_rendition_key(parent_storage_key: str, rend_name: str) -> str:
    """Derive a rendition storage key from a parent storage key.

    E.g. 'images/uuid.webp', 'thumbnail' -> 'images/uuid_t300.webp'.
    Preserves parent key extension or defaults to 'webp'.
    """
    prefix, _, filename = parent_storage_key.rpartition("/")
    if "." in filename:
        stem, ext = filename.rsplit(".", 1)
        ext_part = f".{ext}"
    else:
        stem = filename
        ext_part = ".webp"
    suffix = _RENDITION_SUFFIXES.get(rend_name, rend_name)
    file_part = f"{stem}_{suffix}{ext_part}"
    return f"{prefix}/{file_part}" if prefix else file_part


def derive_thumbnail_url(storage_key: str, renditions: dict[str, str] | None = None) -> str:
    """Derive the canonical thumbnail URL for an image.

    If a materialized thumbnail rendition exists:
    - On object storage with a public base URL, emits the direct CDN object URL.
    - Otherwise (local or no public base URL), emits signed imgproxy with 'rs:auto'.

    If no materialized rendition exists (pre-existing records):
    - Falls back to signed imgproxy with 'rs:fill:300:300:0/g:no'.
    """
    from app.services.imgproxy import signed_image_url
    from app.services.storage import has_public_base_url, public_object_url

    thumb_key = renditions.get("thumbnail") if renditions else None
    if thumb_key:
        if has_public_base_url():
            return public_object_url(thumb_key)
        return signed_image_url(thumb_key, processing_options="rs:auto")
    return signed_image_url(storage_key, processing_options="rs:fill:300:300:0/g:no")
