"""Tests for materialized image renditions:

1. Materialized 300x300 thumbnail generated during upload in the same libvips pass.
2. Stored under derived keys (images/<uuid>_t300.webp) and recorded in `renditions`.
3. `thumbnail_url` in `to_public()` emits direct CDN URL when `has_public_base_url()` is True,
   and falls back cleanly to imgproxy for pre-existing rows without renditions.
4. Private images withhold accelerator URLs but allow serving renditions via
   `/files/{id}/download?rendition=thumb` (owner-scoped or bound read:file token).
5. Public -> private PATCH rotates both parent and rendition storage keys, deleting old objects.
6. DELETE /files/{id} cascades deletion to all rendition storage objects.
"""

from __future__ import annotations

import logging
import time

import httpx
import jwt
import pytest

import app.services.storage as storage_module
from app.config import _derive_owner, settings
from app.services.metadata import KIND_IMAGE
from tests.conftest import fixture_bytes
from tests.fakes import InMemoryMetadataStore, InMemoryStorageBackend

OWNER = _derive_owner("test-token")


async def test_image_upload_generates_and_stores_thumbnail_rendition(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
) -> None:
    # With ?thumbnail=true, generates thumbnail
    resp = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    assert resp.status_code == 200
    body = resp.json()

    record = await fake_metadata.get(body["id"], OWNER)
    assert record is not None
    assert record.renditions is not None
    assert "thumbnail" in record.renditions

    thumb_key = record.renditions["thumbnail"]
    assert thumb_key.startswith("images/")
    assert thumb_key.endswith("_t300.webp")

    # Main image and thumbnail objects exist in storage
    assert record.storage_key in fake_storage.objects
    assert thumb_key in fake_storage.objects
    assert fake_storage.objects[thumb_key][:4] == b"RIFF"

    # Default upload (thumbnail=False) does not generate a rendition
    resp_default = await client.post(
        "/upload/image",
        headers=auth_headers,
        files={"file": ("tiny2.png", fixture_bytes("tiny.png"), "image/png")},
    )
    assert resp_default.status_code == 200
    body_default = resp_default.json()
    record_default = await fake_metadata.get(body_default["id"], OWNER)
    assert record_default is not None
    assert record_default.renditions == {}


async def test_to_public_emits_direct_cdn_thumbnail_when_public_base_url_set(
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(settings, "S3_PUBLIC_BASE_URL", "https://cdn.example.com")
    monkeypatch.setattr(
        storage_module, "_storage", InMemoryStorageBackend(base_url="https://cdn.example.com")
    )

    # 1. Record with materialized thumbnail rendition
    rec = await fake_metadata.create(
        owner=OWNER,
        kind=KIND_IMAGE,
        storage_key="images/abc.webp",
        content_type="image/webp",
        size_bytes=100,
        status="ready",
        visibility="public",
        renditions={"thumbnail": "images/abc_t300.webp"},
    )
    public_view = rec.to_public()
    assert public_view["url"] == "https://cdn.example.com/images/abc.webp"
    assert public_view["thumbnail_url"] == "https://cdn.example.com/images/abc_t300.webp"

    # 2. Pre-existing record without renditions (backward compatibility fallback)
    old_rec = await fake_metadata.create(
        owner=OWNER,
        kind=KIND_IMAGE,
        storage_key="images/old.webp",
        content_type="image/webp",
        size_bytes=100,
        status="ready",
        visibility="public",
        renditions=None,
    )
    old_public_view = old_rec.to_public()
    assert old_public_view["url"] == "https://cdn.example.com/images/old.webp"
    assert "/rs:fill:300:300:0/g:no/" in old_public_view["thumbnail_url"]


async def test_private_image_withholds_accelerators_and_serves_rendition_via_download(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    rec = await fake_metadata.create(
        owner=OWNER,
        kind=KIND_IMAGE,
        storage_key="images/sec.webp",
        content_type="image/webp",
        size_bytes=100,
        status="ready",
        visibility="private",
        renditions={"thumbnail": "images/sec_t300.webp"},
    )

    # Accelerators withheld in to_public()
    public_view = rec.to_public()
    assert public_view["url"].endswith(f"/files/{rec.id}/download")
    assert "thumbnail_url" not in public_view

    # Authorized download with ?rendition=thumb
    resp = await client.get(f"/files/{rec.id}/download?rendition=thumb", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.headers["X-Accel-Redirect"] == "/internal-media/images/sec_t300.webp"
    assert resp.headers["content-type"] == "image/webp"

    # Also accepts 'thumbnail' and 't300'
    resp_t = await client.get(f"/files/{rec.id}/download?rendition=thumbnail", headers=auth_headers)
    assert resp_t.status_code == 200
    assert resp_t.headers["X-Accel-Redirect"] == "/internal-media/images/sec_t300.webp"

    # Unauthorized download 404s
    assert (await client.get(f"/files/{rec.id}/download?rendition=thumb")).status_code == 404

    # Other owner 404s
    other = await fake_metadata.create(
        owner="other-user",
        kind=KIND_IMAGE,
        storage_key="images/other.webp",
        content_type="image/webp",
        size_bytes=100,
        status="ready",
        visibility="private",
        renditions={"thumbnail": "images/other_t300.webp"},
    )
    resp_other = await client.get(
        f"/files/{other.id}/download?rendition=thumb", headers=auth_headers
    )
    assert resp_other.status_code == 404


async def test_private_image_rendition_readable_with_bound_jwt_token(
    client: httpx.AsyncClient,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "unit-test-jwt-secret-that-is-long-enough-for-hs256"
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", secret)

    rec = await fake_metadata.create(
        owner=OWNER,
        kind=KIND_IMAGE,
        storage_key="images/bound.webp",
        content_type="image/webp",
        size_bytes=100,
        status="ready",
        visibility="private",
        renditions={"thumbnail": "images/bound_t300.webp"},
    )

    now = int(time.time())
    token = jwt.encode(
        {"sub": OWNER, "scopes": ["read:file"], "file": rec.id, "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )

    resp = await client.get(f"/files/{rec.id}/download?rendition=thumb&token={token}")
    assert resp.status_code == 200
    assert resp.headers["X-Accel-Redirect"] == "/internal-media/images/bound_t300.webp"


async def test_download_unsupported_or_missing_rendition_errors(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    rec_with_thumb = await fake_metadata.create(
        owner=OWNER,
        kind=KIND_IMAGE,
        storage_key="images/a.webp",
        content_type="image/webp",
        size_bytes=100,
        status="ready",
        visibility="public",
        renditions={"thumbnail": "images/a_t300.webp"},
    )
    rec_no_renditions = await fake_metadata.create(
        owner=OWNER,
        kind=KIND_IMAGE,
        storage_key="images/b.webp",
        content_type="image/webp",
        size_bytes=100,
        status="ready",
        visibility="public",
        renditions=None,
    )

    # Unsupported rendition name -> 400
    bad = await client.get(f"/files/{rec_with_thumb.id}/download?rendition=huge")
    assert bad.status_code == 400

    # Missing rendition on old record -> 404
    missing = await client.get(f"/files/{rec_no_renditions.id}/download?rendition=thumb")
    assert missing.status_code == 404


async def test_public_to_private_rotation_cascades_to_renditions(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_storage: InMemoryStorageBackend,
) -> None:
    await fake_storage.upload(b"main-bytes", "images/orig.webp", "image/webp")
    await fake_storage.upload(b"thumb-bytes", "images/orig_t300.webp", "image/webp")

    rec = await fake_metadata.create(
        owner=OWNER,
        kind=KIND_IMAGE,
        storage_key="images/orig.webp",
        content_type="image/webp",
        size_bytes=10,
        status="ready",
        visibility="public",
        renditions={"thumbnail": "images/orig_t300.webp"},
    )

    resp = await client.patch(
        f"/files/{rec.id}", headers=auth_headers, json={"visibility": "private"}
    )
    assert resp.status_code == 200

    updated = await fake_metadata.get(rec.id, OWNER)
    assert updated is not None
    assert updated.visibility == "private"
    assert updated.storage_key != "images/orig.webp"
    assert updated.renditions is not None
    assert updated.renditions["thumbnail"] != "images/orig_t300.webp"
    assert updated.renditions["thumbnail"].endswith("_t300.webp")

    new_thumb_key = updated.renditions["thumbnail"]

    # New objects present with identical bytes
    assert fake_storage.objects[updated.storage_key] == b"main-bytes"
    assert fake_storage.objects[new_thumb_key] == b"thumb-bytes"

    # Old objects deleted
    assert "images/orig.webp" not in fake_storage.objects
    assert "images/orig_t300.webp" not in fake_storage.objects
    assert "images/orig.webp" in fake_storage.deleted_keys
    assert "images/orig_t300.webp" in fake_storage.deleted_keys


async def test_delete_cascades_to_renditions(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_storage: InMemoryStorageBackend,
) -> None:
    await fake_storage.upload(b"main", "images/del.webp", "image/webp")
    await fake_storage.upload(b"thumb", "images/del_t300.webp", "image/webp")

    rec = await fake_metadata.create(
        owner=OWNER,
        kind=KIND_IMAGE,
        storage_key="images/del.webp",
        content_type="image/webp",
        size_bytes=4,
        status="ready",
        visibility="public",
        renditions={"thumbnail": "images/del_t300.webp"},
    )

    resp = await client.delete(f"/files/{rec.id}", headers=auth_headers)
    assert resp.status_code == 204

    # Both objects deleted from storage
    assert "images/del.webp" not in fake_storage.objects
    assert "images/del_t300.webp" not in fake_storage.objects
    assert "images/del.webp" in fake_storage.deleted_keys
    assert "images/del_t300.webp" in fake_storage.deleted_keys

    # Row deleted from store
    assert await fake_metadata.get(rec.id, OWNER) is None


async def test_share_token_serves_rendition(
    client: httpx.AsyncClient,
    fake_metadata: InMemoryMetadataStore,
) -> None:
    image = await fake_metadata.create(
        owner=OWNER,
        kind=KIND_IMAGE,
        storage_key="images/s.webp",
        content_type="image/webp",
        size_bytes=10,
        status="ready",
        visibility="private",
        renditions={"thumbnail": "images/s_t300.webp"},
    )
    await fake_metadata.set_share_token(image.id, OWNER, "share-img-tok")

    resp = await client.get("/share/share-img-tok?rendition=thumb")
    assert resp.status_code == 200
    assert resp.headers["X-Accel-Redirect"] == "/internal-media/images/s_t300.webp"


async def test_upload_and_record_thumbnail_urls_are_byte_identical(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """R1: The upload response and the record view must return byte-identical thumbnail URLs."""
    resp = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    assert resp.status_code == 200
    body = resp.json()
    upload_thumb_url = body["thumbnail_url"]

    # Fetch via GET /files/{id}
    rec_resp = await client.get(f"/files/{body['id']}", headers=auth_headers)
    assert rec_resp.status_code == 200
    rec_body = rec_resp.json()
    record_thumb_url = rec_body["thumbnail_url"]

    assert upload_thumb_url == record_thumb_url, (
        f"Thumbnail URLs must match byte-for-byte: {upload_thumb_url!r} != {record_thumb_url!r}"
    )


async def test_upload_response_carries_url_without_a_followup_get(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2: on a public-base-url-configured backend, POST /upload/image alone
    (no follow-up GET /files/{id}) must be enough to render the image: the
    full-size direct url and the thumbnail."""
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(settings, "S3_PUBLIC_BASE_URL", "https://cdn.example.com")
    monkeypatch.setattr(
        storage_module, "_storage", InMemoryStorageBackend(base_url="https://cdn.example.com")
    )

    resp = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["url"].startswith("https://cdn.example.com/images/")
    assert body["url"].endswith(".webp")
    assert body["thumbnail_url"].startswith("https://cdn.example.com/images/")
    assert body["thumbnail_url"].endswith("_t300.webp")

    # Matches GET /files/{id} exactly -- no separate resolution path.
    record_view = (await client.get(f"/files/{body['id']}", headers=auth_headers)).json()
    assert record_view["url"] == body["url"]
    assert record_view["thumbnail_url"] == body["thumbnail_url"]


def test_derive_rendition_key_format_independent_of_parent() -> None:
    """Rendition format is determined by the rendition spec (.webp), not the parent extension."""
    from app.services.renditions import (
        RENDITION_SPECS,
        derive_rendition_key,
        get_rendition_spec,
    )

    # Spec registry assertions
    assert "thumbnail" in RENDITION_SPECS
    spec = get_rendition_spec("thumbnail")
    assert spec is not None
    assert RENDITION_SPECS["thumbnail"] == spec
    assert spec.name == "thumbnail"
    assert spec.suffix == "t300"
    assert spec.format == "webp"
    assert spec.width == 300
    assert spec.height == 300
    assert get_rendition_spec("thumb") == spec
    assert get_rendition_spec("t300") == spec
    assert get_rendition_spec("unknown") is None

    # Key derivation always emits .webp regardless of parent extension
    assert derive_rendition_key("images/test.png", "thumbnail") == "images/test_t300.webp"
    assert derive_rendition_key("images/test.webp", "thumbnail") == "images/test_t300.webp"
    assert (
        derive_rendition_key("videos/x_compressed.mp4", "thumbnail")
        == "videos/x_compressed_t300.webp"
    )
    assert derive_rendition_key("files/document.pdf", "thumbnail") == "files/document_t300.webp"
    assert derive_rendition_key("test.webp", "thumbnail") == "test_t300.webp"
    assert derive_rendition_key("images/noext", "thumbnail") == "images/noext_t300.webp"
    assert derive_rendition_key("images/test.webp", "thumb") == "images/test_t300.webp"
    assert derive_rendition_key("images/test.webp", "t300") == "images/test_t300.webp"


async def test_private_image_upload_withholds_imgproxy_urls(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    """POST /upload/image must apply the same visibility rule as GET /files/{id}.

    An imgproxy URL carries no ownership check and never expires, and it
    resolves -- imgproxy reads the shared volume (or a public bucket) directly.
    Returning one for a `private` upload handed the owner a permanent
    unauthenticated way to read media they had just marked private, and made the
    two views of the same record disagree.
    """
    private = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
        data={"visibility": "private"},
    )
    assert private.status_code == 200
    body = private.json()
    assert "custom_url" not in body
    assert "thumbnail_url" not in body
    assert "storage_key" not in body
    assert "renditions" not in body
    assert body["id"]

    # The record view agrees, and the app route is still the way in.
    record = (await client.get(f"/files/{body['id']}", headers=auth_headers)).json()
    assert "thumbnail_url" not in record
    assert "storage_key" not in record
    assert "renditions" not in record
    assert record["url"].endswith(f"/files/{body['id']}/download")

    # A public upload is unaffected, and carries the thumbnail URL and keys when requested
    public = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
        data={"visibility": "public"},
    )
    public_body = public.json()
    assert "thumbnail_url" in public_body
    assert public_body["thumbnail_url"].endswith(".webp")
    assert "storage_key" in public_body
    assert "renditions" in public_body


def _make_image(width: int, height: int) -> bytes:
    import pyvips

    img = pyvips.Image.black(width, height)
    return img.write_to_buffer(".png")


async def test_to_public_and_upload_include_storage_key_and_renditions(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """storage_key and renditions are present on to_public(), and url equals
    {public_base}/{storage_key} when a public base URL is configured —
    i.e. a consumer can reproduce the URL from the key alone."""
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(settings, "S3_PUBLIC_BASE_URL", "https://cdn.example.com")
    monkeypatch.setattr(
        storage_module, "_storage", InMemoryStorageBackend(base_url="https://cdn.example.com")
    )

    img_bytes = _make_image(1920, 1080)
    resp = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("photo.png", img_bytes, "image/png")},
    )
    assert resp.status_code == 200
    body = resp.json()

    assert "storage_key" in body
    assert body["storage_key"].startswith("images/")
    assert body["storage_key"].endswith(".webp")
    assert body["url"] == f"https://cdn.example.com/{body['storage_key']}"

    assert "renditions" in body
    renditions = body["renditions"]
    assert "thumbnail" in renditions
    assert "w400" in renditions
    assert "w800" in renditions
    assert "w1600" in renditions
    assert renditions["w400"].endswith("_w400.webp")
    assert renditions["w800"].endswith("_w800.webp")
    assert renditions["w1600"].endswith("_w1600.webp")
    assert renditions["thumbnail"].endswith("_t300.webp")

    # GET /files/{id} matches upload response
    rec_resp = await client.get(f"/files/{body['id']}", headers=auth_headers)
    assert rec_resp.status_code == 200
    rec_body = rec_resp.json()
    assert rec_body["storage_key"] == body["storage_key"]
    assert rec_body["renditions"] == body["renditions"]
    assert rec_body["url"] == body["url"]


async def test_widths_above_source_image_width_are_omitted(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_storage: InMemoryStorageBackend,
) -> None:
    """Widths above the source image's width are omitted (upload a 500px-wide
    image -> w400 is produced, but no w800, no w1600)."""
    monkeypatch.setattr(settings, "IMAGE_RENDITION_MODE", "materialize")

    img_bytes = _make_image(500, 300)
    resp = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("medium.png", img_bytes, "image/png")},
    )
    assert resp.status_code == 200
    body = resp.json()

    renditions = body["renditions"]
    assert "thumbnail" in renditions
    assert "w400" in renditions
    assert "w800" not in renditions
    assert "w1600" not in renditions

    # Only thumbnail and w400 exist in storage, not w800 or w1600
    assert renditions["thumbnail"] in fake_storage.objects
    assert renditions["w400"] in fake_storage.objects
    assert not any("_w800" in k for k in fake_storage.objects)
    assert not any("_w1600" in k for k in fake_storage.objects)

    # GET /files/{id} agrees
    rec_resp = await client.get(f"/files/{body['id']}", headers=auth_headers)
    assert rec_resp.status_code == 200
    rec_body = rec_resp.json()
    assert "w400" in rec_body["renditions"]
    assert "w800" not in rec_body["renditions"]
    assert "w1600" not in rec_body["renditions"]


async def test_materialize_vs_on_demand_mode(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_storage: InMemoryStorageBackend,
) -> None:
    """materialize stores rendition objects and populates renditions; on_demand
    stores nothing extra and leaves renditions empty, while url/thumbnail_url still resolve."""
    img_bytes_mat = _make_image(1920, 1080)
    img_bytes_ondemand = _make_image(1920, 1081)

    # 1. materialize mode (default)
    monkeypatch.setattr(settings, "IMAGE_RENDITION_MODE", "materialize")
    resp_mat = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("mat.png", img_bytes_mat, "image/png")},
    )
    assert resp_mat.status_code == 200
    body_mat = resp_mat.json()
    assert body_mat["renditions"]
    assert len(body_mat["renditions"]) == 4  # thumbnail, w400, w800, w1600
    for rend_key in body_mat["renditions"].values():
        assert rend_key in fake_storage.objects

    # 2. on_demand mode
    monkeypatch.setattr(settings, "IMAGE_RENDITION_MODE", "on_demand")
    resp_ondemand = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("ondemand.png", img_bytes_ondemand, "image/png")},
    )
    assert resp_ondemand.status_code == 200
    body_ondemand = resp_ondemand.json()
    assert body_ondemand["renditions"] == {}
    assert "url" in body_ondemand
    assert "thumbnail_url" in body_ondemand
    # Only the primary image is in storage
    assert body_ondemand["storage_key"] in fake_storage.objects
    assert not any(
        k.endswith(("_t300.webp", "_w400.webp", "_w800.webp", "_w1600.webp"))
        and k != body_mat["storage_key"]
        for k in fake_storage.objects
        if k.startswith("images/") and k not in body_mat["renditions"].values()
    )


async def test_mode_flipping_compatibility(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flipping IMAGE_RENDITION_MODE does not break records created under the other mode."""
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(settings, "S3_PUBLIC_BASE_URL", "https://cdn.example.com")
    monkeypatch.setattr(
        storage_module, "_storage", InMemoryStorageBackend(base_url="https://cdn.example.com")
    )

    img_bytes_mat = _make_image(1920, 1080)
    img_bytes_ondemand = _make_image(1920, 1081)

    # 1. Upload in materialize mode
    monkeypatch.setattr(settings, "IMAGE_RENDITION_MODE", "materialize")
    resp_mat = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("mat.png", img_bytes_mat, "image/png")},
    )
    mat_id = resp_mat.json()["id"]

    # 2. Upload in on_demand mode
    monkeypatch.setattr(settings, "IMAGE_RENDITION_MODE", "on_demand")
    resp_ondemand = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("ondemand.png", img_bytes_ondemand, "image/png")},
    )
    ondemand_id = resp_ondemand.json()["id"]

    # Flip to on_demand mode and read materialize record
    monkeypatch.setattr(settings, "IMAGE_RENDITION_MODE", "on_demand")
    mat_read = (await client.get(f"/files/{mat_id}", headers=auth_headers)).json()
    assert mat_read["renditions"]
    assert "w400" in mat_read["renditions"]
    assert mat_read["thumbnail_url"].endswith("_t300.webp")

    # Flip to materialize mode and read on_demand record
    monkeypatch.setattr(settings, "IMAGE_RENDITION_MODE", "materialize")
    ondemand_read = (await client.get(f"/files/{ondemand_id}", headers=auth_headers)).json()
    assert ondemand_read["renditions"] == {}
    assert "url" in ondemand_read
    assert "thumbnail_url" in ondemand_read


def test_invalid_image_rendition_mode_fails_fast() -> None:
    """An invalid IMAGE_RENDITION_MODE fails fast at startup."""
    from app.config import Settings

    with pytest.raises(ValueError, match="IMAGE_RENDITION_MODE"):
        Settings(
            FILE_MANAGER_BEARER_TOKENS="tok",
            IMGPROXY_KEY="00" * 32,
            IMGPROXY_SALT="11" * 32,
            IMAGE_RENDITION_MODE="invalid_mode",  # type: ignore[arg-type]
        )


async def test_delete_cascades_to_all_materialized_widths(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_storage: InMemoryStorageBackend,
) -> None:
    """DELETE /files/{id} removes every materialized width."""
    monkeypatch.setattr(settings, "IMAGE_RENDITION_MODE", "materialize")

    img_bytes = _make_image(1920, 1080)
    resp = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("full.png", img_bytes, "image/png")},
    )
    body = resp.json()
    renditions = body["renditions"]
    assert len(renditions) == 4
    for key in renditions.values():
        assert key in fake_storage.objects

    # Delete the record
    del_resp = await client.delete(f"/files/{body['id']}", headers=auth_headers)
    assert del_resp.status_code == 204

    # Main object and all 4 renditions deleted
    assert body["storage_key"] not in fake_storage.objects
    assert body["storage_key"] in fake_storage.deleted_keys
    for key in renditions.values():
        assert key not in fake_storage.objects
        assert key in fake_storage.deleted_keys


async def test_private_visibility_rotation_cascades_to_all_materialized_widths(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
) -> None:
    """PATCH /files/{id} to private rotates all materialized width rendition keys under private/."""
    monkeypatch.setattr(settings, "IMAGE_RENDITION_MODE", "materialize")

    img_bytes = _make_image(1920, 1080)
    resp = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("full.png", img_bytes, "image/png")},
    )
    body = resp.json()
    old_renditions = body["renditions"]
    old_storage_key = body["storage_key"]

    patch_resp = await client.patch(
        f"/files/{body['id']}",
        headers=auth_headers,
        json={"visibility": "private"},
    )
    assert patch_resp.status_code == 200
    patched_body = patch_resp.json()

    # The response for a private record must NOT contain storage_key or renditions
    assert "storage_key" not in patched_body
    assert "renditions" not in patched_body

    # Check store for rotated keys
    updated_rec = await fake_metadata.get(body["id"], OWNER)
    assert updated_rec is not None
    assert updated_rec.visibility == "private"
    assert updated_rec.storage_key != old_storage_key
    assert updated_rec.storage_key.startswith("private/images/")
    new_renditions = updated_rec.renditions
    assert new_renditions is not None
    assert len(new_renditions) == 4
    for name, old_key in old_renditions.items():
        new_key = new_renditions[name]
        assert new_key != old_key
        assert new_key.startswith("private/images/")
        assert old_key in fake_storage.deleted_keys
        assert new_key in fake_storage.objects


async def test_download_and_share_all_width_renditions(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private record serves all width renditions via /download?rendition= and /share/{token}."""
    monkeypatch.setattr(settings, "IMAGE_RENDITION_MODE", "materialize")

    img_bytes = _make_image(1920, 1080)
    resp = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("full.png", img_bytes, "image/png")},
        data={"visibility": "private"},
    )
    body = resp.json()
    rec_id = body["id"]

    # Download with w400, w800, w1600 and alias 400
    for rend_arg in ("w400", "400", "w800", "800", "w1600", "1600"):
        d_resp = await client.get(
            f"/files/{rec_id}/download?rendition={rend_arg}", headers=auth_headers
        )
        assert d_resp.status_code == 200
        assert d_resp.headers["content-type"] == "image/webp"

    # Share link serves w400
    share_resp = await client.post(f"/files/{rec_id}/share", headers=auth_headers)
    assert share_resp.status_code == 200
    token = share_resp.json()["share_token"]

    share_get = await client.get(f"/share/{token}?rendition=w400")
    assert share_get.status_code == 200
    assert share_get.headers["content-type"] == "image/webp"


def test_rendition_specs_and_aliases_registry() -> None:
    """Check registered specs and aliases for responsive widths."""
    from app.services.renditions import (
        ALLOWED_RENDITION_NAMES,
        RENDITION_SPECS,
        get_rendition_spec,
        normalize_rendition_name,
    )

    for name in ("thumbnail", "w400", "w800", "w1600"):
        assert name in RENDITION_SPECS
        spec = get_rendition_spec(name)
        assert spec is not None
        assert spec.name == name

    assert get_rendition_spec("w400").width == 400
    assert get_rendition_spec("w400").crop is False
    assert get_rendition_spec("w800").width == 800
    assert get_rendition_spec("w800").crop is False
    assert get_rendition_spec("w1600").width == 1600
    assert get_rendition_spec("w1600").crop is False

    assert normalize_rendition_name("400") == "w400"
    assert normalize_rendition_name("800") == "w800"
    assert normalize_rendition_name("1600") == "w1600"
    assert "400" in ALLOWED_RENDITION_NAMES
    assert "800" in ALLOWED_RENDITION_NAMES
    assert "1600" in ALLOWED_RENDITION_NAMES


async def test_on_demand_mode_disabled_cache_emits_startup_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Lifespan emits a warning when on_demand mode is set and cache is disabled."""
    import app.services.metadata as metadata_module
    from app.main import app, lifespan

    store = InMemoryMetadataStore()
    monkeypatch.setattr(metadata_module, "_store", store)
    monkeypatch.setattr(settings, "FILE_MANAGER_BEARER_TOKENS", "label:secret")
    monkeypatch.setattr(settings, "IMAGE_RENDITION_MODE", "on_demand")
    monkeypatch.setattr(settings, "ENABLE_IMGPROXY_CACHE", "false")

    with caplog.at_level(logging.WARNING):
        async with lifespan(app):
            pass

    expected_msg = (
        "IMAGE_RENDITION_MODE=on_demand but ENABLE_IMGPROXY_CACHE is disabled: "
        "imgproxy will serve responsive image widths without NGINX origin shielding"
    )
    assert any(expected_msg in rec.message for rec in caplog.records)


async def test_private_and_nonready_records_withhold_storage_key_and_renditions(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    # 1. Private image upload omits storage_key and renditions
    priv_resp = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("img.png", fixture_bytes("tiny.png"), "image/png")},
        data={"visibility": "private"},
    )
    assert priv_resp.status_code == 200
    priv_body = priv_resp.json()
    assert "storage_key" not in priv_body
    assert "renditions" not in priv_body

    # 2. GET /files/{id} for private image omits both
    priv_get = (await client.get(f"/files/{priv_body['id']}", headers=auth_headers)).json()
    assert "storage_key" not in priv_get
    assert "renditions" not in priv_get

    # 3. Bulk image upload with private visibility omits both on items
    bulk_resp = await client.post(
        "/upload/images?thumbnail=true",
        headers=auth_headers,
        files=[("files", ("img1.png", fixture_bytes("tiny.png"), "image/png"))],
        data={"visibility": "private"},
    )
    assert bulk_resp.status_code == 200
    bulk_item = bulk_resp.json()["items"][0]
    assert bulk_item["status"] == "success"
    assert "storage_key" not in bulk_item
    assert "renditions" not in bulk_item

    # 4. Public ready image contains both
    pub_resp = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("img2.png", fixture_bytes("tiny.png"), "image/png")},
        data={"visibility": "public"},
    )
    pub_body = pub_resp.json()
    assert "storage_key" in pub_body
    assert "renditions" in pub_body
    pub_get = (await client.get(f"/files/{pub_body['id']}", headers=auth_headers)).json()
    assert "storage_key" in pub_get
    assert "renditions" in pub_get

    # 5. Non-ready record (status != ready) omits both even when public
    proc_rec = await fake_metadata.create(
        owner=OWNER,
        kind="video",
        storage_key="raw/videos/raw.mp4",
        content_type="video/mp4",
        size_bytes=100,
        status="processing",
        visibility="public",
    )
    proc_public = proc_rec.to_public()
    assert "storage_key" not in proc_public
    assert "renditions" not in proc_public
    proc_get = (await client.get(f"/files/{proc_rec.id}", headers=auth_headers)).json()
    assert "storage_key" not in proc_get
    assert "renditions" not in proc_get

    # 6. Flip public -> private via PATCH /files/{id} stops exposing both
    patch_priv = await client.patch(
        f"/files/{pub_body['id']}",
        headers=auth_headers,
        json={"visibility": "private"},
    )
    assert patch_priv.status_code == 200
    patch_priv_body = patch_priv.json()
    assert "storage_key" not in patch_priv_body
    assert "renditions" not in patch_priv_body
    after_flip = (await client.get(f"/files/{pub_body['id']}", headers=auth_headers)).json()
    assert "storage_key" not in after_flip
    assert "renditions" not in after_flip

    # 7. Flip private -> public via PATCH /files/{id} starts exposing both under images/
    patch_pub = await client.patch(
        f"/files/{pub_body['id']}",
        headers=auth_headers,
        json={"visibility": "public"},
    )
    assert patch_pub.status_code == 200
    patch_pub_body = patch_pub.json()
    assert "storage_key" in patch_pub_body
    assert "renditions" in patch_pub_body
    assert not patch_pub_body["storage_key"].startswith("private/")
