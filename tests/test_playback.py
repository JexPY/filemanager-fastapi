"""GET /files/{id}/download and GET /share/{token} -- the unified, backend-
agnostic playback resolver and the visibility/auth split.

The X-Accel/Range byte transfer itself is nginx's job and is an integration check
against the running stack (see the verification log); here we assert the *shape*
of resolution: the X-Accel header for local, a 302 + Location for s3/public, and
the visibility/owner matrix.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import _derive_owner, settings
from app.services.metadata import UploadRecord
from tests.fakes import InMemoryMetadataStore, InMemoryStorageBackend

OWNER = _derive_owner("test-token")


async def _seed_video(
    store: InMemoryMetadataStore,
    *,
    owner: str = OWNER,
    visibility: str = "private",
    key: str = "videos/v_compressed.mp4",
) -> UploadRecord:
    rec = await store.create(
        owner=owner,
        kind="video",
        storage_key=key,
        content_type="video/mp4",
        size_bytes=1,
        status="ready",
    )
    if visibility != "private":
        updated = await store.set_visibility(rec.id, owner, visibility)
        assert updated is not None
        rec = updated
    return rec


# --- local backend: X-Accel-Redirect (default STORAGE_BACKEND=local/xaccel) ---


async def test_local_public_download_is_xaccel_no_token(
    client: httpx.AsyncClient,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Local always serves a public video via nginx X-Accel -- the media volume
    # has no public URL to 302 to. LOCAL_PUBLIC_BASE_URL is irrelevant here (the
    # set-anyway case is the regression test below); the public_url() 302 branch
    # is s3/gcp only.
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(settings, "LOCAL_PUBLIC_BASE_URL", "")
    rec = await _seed_video(fake_metadata, visibility="public")
    resp = await client.get(f"/files/{rec.id}/download")  # no auth
    assert resp.status_code == 200
    assert resp.headers["X-Accel-Redirect"] == "/internal-media/videos/v_compressed.mp4"
    assert resp.headers["content-type"] == "video/mp4"
    assert resp.content == b""  # app emits no bytes; nginx fills the body


async def test_local_public_download_stays_xaccel_even_with_public_base(
    client: httpx.AsyncClient,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: LOCAL_PUBLIC_BASE_URL set to a base nginx doesn't serve (e.g.
    # the entry proxy origin itself) must NOT turn local public playback into a
    # 302 to a dead /videos/<key> URL. Local always resolves via X-Accel.
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(settings, "LOCAL_PUBLIC_BASE_URL", "http://localhost:9000")
    rec = await _seed_video(fake_metadata, visibility="public")
    resp = await client.get(f"/files/{rec.id}/download")  # no auth
    assert resp.status_code == 200
    assert resp.headers["X-Accel-Redirect"] == "/internal-media/videos/v_compressed.mp4"


async def test_local_private_download_requires_owner(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    rec = await _seed_video(fake_metadata, visibility="private")
    # Owner: served.
    ok = await client.get(f"/files/{rec.id}/download", headers=auth_headers)
    assert ok.status_code == 200
    assert ok.headers["X-Accel-Redirect"].endswith("videos/v_compressed.mp4")
    # No token: 404 (existence never leaks).
    assert (await client.get(f"/files/{rec.id}/download")).status_code == 404


async def test_local_private_download_other_owner_404(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    other = await _seed_video(fake_metadata, owner="someone-else", visibility="private")
    resp = await client.get(f"/files/{other.id}/download", headers=auth_headers)
    assert resp.status_code == 404


async def test_download_non_video_is_400(
    client: httpx.AsyncClient, fake_metadata: InMemoryMetadataStore
) -> None:
    image = await fake_metadata.create(
        owner=OWNER,
        kind="image",
        storage_key="images/a.webp",
        content_type="image/webp",
        size_bytes=1,
        status="ready",
    )
    assert (await client.get(f"/files/{image.id}/download")).status_code == 400


async def test_download_unknown_id_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/files/nope/download")).status_code == 404


# --- object store (s3/gcp) + a public base -> 302 to the stable URL -----------


async def test_public_with_public_base_302s_to_stable_url(
    client: httpx.AsyncClient,
    fake_metadata: InMemoryMetadataStore,
    fake_storage: InMemoryStorageBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An object-store backend with a public/CDN base -> a plain 302 to the stable
    # public_url(), tokenless, no per-request presigning. Local is excluded (it
    # has no public URL), so this is s3/gcp only. The URL comes from the backend's
    # public_url(); for the fake that's its own base.
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(settings, "S3_PUBLIC_BASE_URL", "https://cdn.example.com")
    rec = await _seed_video(fake_metadata, visibility="public")
    resp = await client.get(f"/files/{rec.id}/download")  # no token
    assert resp.status_code == 302
    assert resp.headers["location"] == fake_storage.public_url("videos/v_compressed.mp4")


# --- s3 backend: 302 to a freshly-minted presigned URL ------------------------


async def test_s3_private_download_302s_to_presigned(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_storage: InMemoryStorageBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    fake_storage._presign_capable = True
    rec = await _seed_video(fake_metadata, visibility="private")
    resp = await client.get(f"/files/{rec.id}/download", headers=auth_headers)
    assert resp.status_code == 302
    assert "X-Amz-Signature=fake" in resp.headers["location"]


# --- share token: serves regardless of visibility, revocable ------------------


async def test_share_token_serves_private_video_no_token(
    client: httpx.AsyncClient, fake_metadata: InMemoryMetadataStore
) -> None:
    rec = await _seed_video(fake_metadata, visibility="private")
    await fake_metadata.set_share_token(rec.id, OWNER, "share-abc")

    resp = await client.get("/share/share-abc")  # no auth, despite private
    assert resp.status_code == 200
    assert resp.headers["X-Accel-Redirect"].endswith("videos/v_compressed.mp4")

    # Revoke -> the same token now 404s.
    await fake_metadata.clear_share_token(rec.id, OWNER)
    assert (await client.get("/share/share-abc")).status_code == 404


async def test_share_unknown_token_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/share/does-not-exist")).status_code == 404


async def test_share_non_video_token_404(
    client: httpx.AsyncClient, fake_metadata: InMemoryMetadataStore
) -> None:
    image = await fake_metadata.create(
        owner=OWNER,
        kind="image",
        storage_key="images/a.webp",
        content_type="image/webp",
        size_bytes=1,
        status="ready",
    )
    await fake_metadata.set_share_token(image.id, OWNER, "img-token")
    assert (await client.get("/share/img-token")).status_code == 404

