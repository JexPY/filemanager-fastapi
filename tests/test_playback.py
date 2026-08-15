"""GET /files/{id}/download and GET /share/{token} -- the unified, backend-
agnostic playback resolver and the visibility/auth split.

The X-Accel/Range byte transfer itself is nginx's job and is an integration check
against the running stack (see the verification log); here we assert the *shape*
of resolution: the X-Accel header for local, a 302 + Location for s3/public, and
the visibility/owner matrix.
"""

from __future__ import annotations

import time
from urllib.parse import unquote

import httpx
import jwt
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
    content_type: str = "video/mp4",
) -> UploadRecord:
    rec = await store.create(
        owner=owner,
        kind="video",
        storage_key=key,
        content_type=content_type,
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


async def test_local_webm_download_uses_video_webm_content_type(
    client: httpx.AsyncClient,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    rec = await _seed_video(
        fake_metadata,
        visibility="public",
        key="videos/v_compressed.webm",
        content_type="video/webm",
    )
    resp = await client.get(f"/files/{rec.id}/download")
    assert resp.status_code == 200
    assert resp.headers["X-Accel-Redirect"] == "/internal-media/videos/v_compressed.webm"
    assert resp.headers["content-type"] == "video/webm"


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


async def test_download_is_kind_agnostic(
    client: httpx.AsyncClient, fake_metadata: InMemoryMetadataStore
) -> None:
    """An image resolves through the identical route, auth, and byte path a video
    does -- the route inspects visibility, never `kind`. This replaces a test that
    asserted the opposite (non-video -> 400); the video-only gate was what stopped
    the service from storing anything else, and a PDF or audio file added later
    needs no new endpoint because of this."""
    image = await fake_metadata.create(
        owner=OWNER,
        kind="image",
        storage_key="images/a.webp",
        content_type="image/webp",
        size_bytes=1,
        status="ready",
        visibility="public",
    )
    resp = await client.get(f"/files/{image.id}/download")

    assert resp.status_code == 200
    assert resp.headers["X-Accel-Redirect"] == "/internal-media/images/a.webp"
    assert resp.headers["content-type"] == "image/webp"


async def test_download_unknown_id_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/files/nope/download")).status_code == 404


async def test_private_record_readable_with_a_bound_read_token_via_query(
    client: httpx.AsyncClient,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-to-end shape fixcar (or any consuming service) uses: it runs its
    own permission check, mints a short-lived `read:file` JWT bound to one id,
    and hands the browser a URL. The token rides `?token=` because an
    <img src>/<video src> cannot set an Authorization header.

    Also asserts the binding actually holds: the same token must not open a
    second private record belonging to the same owner.
    """
    secret = "unit-test-jwt-secret-that-is-long-enough-for-hs256"
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", secret)

    private = await _seed_video(fake_metadata, visibility="private")
    other = await _seed_video(fake_metadata, visibility="private", key="videos/other.mp4")

    now = int(time.time())
    token = jwt.encode(
        {
            "sub": OWNER,
            "scopes": ["read:file"],
            "file": private.id,
            "iat": now,
            "exp": now + 300,
        },
        secret,
        algorithm="HS256",
    )

    granted = await client.get(f"/files/{private.id}/download?token={token}")
    assert granted.status_code == 200
    assert granted.headers["X-Accel-Redirect"] == "/internal-media/videos/v_compressed.mp4"

    # Bound to one record: 404, not 403 -- existence still must not leak.
    assert (await client.get(f"/files/{other.id}/download?token={token}")).status_code == 404
    # And no token at all is still 404.
    assert (await client.get(f"/files/{private.id}/download")).status_code == 404


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


async def test_s3_presigned_url_carries_record_content_type_and_filename(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_storage: InMemoryStorageBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The object-store 302 must carry the *record's* content type and original
    filename as signed response-header overrides, not defer to whatever metadata
    the stored object happens to have.

    Two real failures this prevents: the `local` backend stores no content-type
    at all, so migrating local -> s3 (rclone infers it from the extension) could
    leave a video as application/octet-stream, which browsers download instead of
    playing; and the local byte paths already set an inline Content-Disposition,
    so without this the 302 silently lost the filename on s3/gcp only.
    """
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    fake_storage._presign_capable = True
    rec = await _seed_video(fake_metadata, visibility="private", content_type="video/webm")
    updated = await fake_metadata.mark_ready(
        rec.id, storage_key=rec.storage_key, size_bytes=1, content_type="video/webm"
    )
    assert updated is not None

    resp = await client.get(f"/files/{rec.id}/download", headers=auth_headers)

    assert resp.status_code == 302
    location = unquote(resp.headers["location"])
    assert "response-content-type=video/webm" in location
    assert 'response-content-disposition=inline; filename="' in location


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


async def test_share_token_serves_any_kind(
    client: httpx.AsyncClient, fake_metadata: InMemoryMetadataStore
) -> None:
    """A share token is the grant *and* the locator, so kind is irrelevant to it.
    Previously a shared image 404'd, which made share links a video-only feature
    for no reason other than the gate."""
    image = await fake_metadata.create(
        owner=OWNER,
        kind="image",
        storage_key="images/a.webp",
        content_type="image/webp",
        size_bytes=1,
        status="ready",
        visibility="private",
    )
    await fake_metadata.set_share_token(image.id, OWNER, "img-token")

    resp = await client.get("/share/img-token")  # no auth, despite private

    assert resp.status_code == 200
    assert resp.headers["X-Accel-Redirect"] == "/internal-media/images/a.webp"


def test_sanitize_content_disposition_filename() -> None:
    from app.routers.utils import _sanitize_content_disposition_filename

    assert (
        _sanitize_content_disposition_filename('evil"name\r\nHeader: inject.mp4')
        == "evil_name__Header: inject.mp4"
    )
    assert _sanitize_content_disposition_filename('test\\file"name.mp4') == "test_file_name.mp4"
    assert _sanitize_content_disposition_filename("") == "download"
    assert len(_sanitize_content_disposition_filename("a" * 300)) == 255


async def test_download_sanitizes_content_disposition_header(
    client: httpx.AsyncClient,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    rec = await fake_metadata.create(
        owner=OWNER,
        kind="video",
        storage_key="videos/v_compressed.mp4",
        content_type="video/mp4",
        size_bytes=1,
        status="ready",
        original_filename='evil"name\r\nHeader: inject.mp4',
    )
    rec = await fake_metadata.set_visibility(rec.id, OWNER, "public")
    assert rec is not None
    resp = await client.get(f"/files/{rec.id}/download")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == 'inline; filename="evil_name__Header: inject.mp4"'
