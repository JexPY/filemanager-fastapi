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
    assert body["id"]

    # The record view agrees, and the app route is still the way in.
    record = (await client.get(f"/files/{body['id']}", headers=auth_headers)).json()
    assert "thumbnail_url" not in record
    assert record["url"].endswith(f"/files/{body['id']}/download")

    # A public upload is unaffected, and carries the thumbnail URL when requested
    public = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
        data={"visibility": "public"},
    )
    public_body = public.json()
    assert "thumbnail_url" in public_body
    assert public_body["thumbnail_url"].endswith(".webp")
