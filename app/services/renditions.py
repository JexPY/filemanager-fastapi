"""Shared helpers, canonical naming, and key derivation for materialized renditions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RenditionSpec:
    """Specification for a materialized rendition.

    Format and dimensions are intrinsic properties of the rendition,
    independent of the parent object's type or extension. ``crop`` picks the
    libvips resize strategy: True center-crops to exactly width x height
    (right for square, avatar-shaped content); False fits *within* the
    width x height box preserving aspect ratio, so a landscape photo keeps its
    full frame instead of losing its edges to a square crop.
    """

    name: str
    suffix: str
    width: int
    height: int
    format: str = "webp"
    mime_type: str = "image/webp"
    quality: int = 80
    crop: bool = True
    # libwebp's compression-effort search, 0 (fastest) to 6 (slowest, smallest
    # file). 6 is the right choice for the *primary* encode (a one-time cost
    # for the asset people actually view/embed) but the wrong default for a
    # materialized rendition: these are accelerators generated synchronously
    # on every upload, and effort=6 on a real photo -- not the tiny test
    # fixtures -- is genuinely slow (libwebp's own benchmarks put 4->6 at
    # roughly 2-4x slower for a low-single-digit-percent size gain). 4 trades
    # a little file size for a large, worthwhile speed win here.
    effort: int = 4


RENDITION_SPECS: dict[str, RenditionSpec] = {
    "thumbnail": RenditionSpec(
        name="thumbnail",
        suffix="t300",
        width=300,
        height=300,
        format="webp",
        mime_type="image/webp",
        quality=80,
        crop=True,
        effort=4,
    ),
}

_RENDITION_ALIASES: dict[str, str] = {
    "thumbnail": "thumbnail",
    "thumb": "thumbnail",
    "t300": "thumbnail",
}

ALLOWED_RENDITION_NAMES = frozenset(_RENDITION_ALIASES.keys())


def normalize_rendition_name(name: str | None) -> str | None:
    """Normalize a user-supplied rendition name or alias to its canonical name.

    Returns None if the rendition name is unknown or unsupported.
    """
    if not name:
        return None
    cleaned = name.strip().lower()
    return _RENDITION_ALIASES.get(cleaned)


def get_rendition_spec(name: str | None) -> RenditionSpec | None:
    """Get the RenditionSpec for a rendition name or alias, or None if unknown."""
    canonical = normalize_rendition_name(name)
    return RENDITION_SPECS.get(canonical) if canonical else None


def derive_rendition_key(parent_storage_key: str, rend_name: str) -> str:
    """Derive a rendition storage key from a parent storage key.

    E.g. 'images/uuid.webp', 'thumbnail' -> 'images/uuid_t300.webp'.
    'videos/uuid_compressed.mp4', 'thumbnail' -> 'videos/uuid_compressed_t300.webp'.

    The rendition's output format is a property of the rendition itself
    (defined in RENDITION_SPECS), not of the parent key.
    """
    spec = get_rendition_spec(rend_name)
    suffix = spec.suffix if spec else rend_name
    ext = spec.format if spec else "webp"

    prefix, _, filename = parent_storage_key.rpartition("/")
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    file_part = f"{stem}_{suffix}.{ext}"
    return f"{prefix}/{file_part}" if prefix else file_part


def _derive_rendition_public_url(
    rendition_name: str,
    fallback_processing_options: str,
    storage_key: str,
    renditions: dict[str, str] | None,
) -> str:
    """Shared resolution for every materialized rendition's public URL.

    If the rendition was materialized (present in `renditions`):
    - On object storage with a public base URL, emits the direct CDN object URL.
    - Otherwise (local or no public base URL), emits signed imgproxy with 'rs:auto'.

    If it was not materialized (pre-existing records from before a given
    rendition spec existed), falls back to a live signed imgproxy transform of
    the *parent* object using `fallback_processing_options`.
    """
    from app.services.imgproxy import signed_image_url
    from app.services.storage import has_public_base_url, public_object_url

    spec = get_rendition_spec(rendition_name)
    fmt = spec.format if spec else "webp"
    rend_key = renditions.get(rendition_name) if renditions else None
    if rend_key:
        if has_public_base_url():
            return public_object_url(rend_key)
        return signed_image_url(rend_key, processing_options="rs:auto", format=fmt)
    return signed_image_url(storage_key, processing_options=fallback_processing_options, format=fmt)


def derive_thumbnail_url(storage_key: str, renditions: dict[str, str] | None = None) -> str:
    """Derive the canonical 300x300 thumbnail URL for an image (materialized
    object when available, else a live imgproxy fallback -- see
    `_derive_rendition_public_url`)."""
    return _derive_rendition_public_url(
        "thumbnail", "rs:fill:300:300:0/g:no", storage_key, renditions
    )
