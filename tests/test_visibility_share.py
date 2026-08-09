"""Owner visibility (PATCH /files/{id}) and share-link management
(POST/DELETE /files/{id}/share). Serving the bytes is covered in test_playback."""

from __future__ import annotations

import httpx

from app.config import _derive_owner
from app.services.metadata import UploadRecord
from tests.fakes import InMemoryMetadataStore

OWNER = _derive_owner("test-token")


async def _seed_video(
    store: InMemoryMetadataStore, owner: str = OWNER, key: str = "videos/v.mp4"
) -> UploadRecord:
    return await store.create(
        owner=owner,
        kind="video",
        storage_key=key,
        content_type="video/mp4",
        size_bytes=1,
        status="ready",
    )


# --- PATCH /files/{id} (visibility) ---------------------------------------


async def test_patch_sets_visibility_public_then_private(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    rec = await _seed_video(fake_metadata)
    assert rec.visibility == "private"

    resp = await client.patch(
        f"/files/{rec.id}", headers=auth_headers, json={"visibility": "public"}
    )
    assert resp.status_code == 200
    assert resp.json()["visibility"] == "public"
    # Persisted on the record.
    assert (await fake_metadata.get(rec.id, OWNER)).visibility == "public"

    resp = await client.patch(
        f"/files/{rec.id}", headers=auth_headers, json={"visibility": "private"}
    )
    assert resp.json()["visibility"] == "private"


async def test_patch_rejects_bad_visibility(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    rec = await _seed_video(fake_metadata)
    resp = await client.patch(
        f"/files/{rec.id}", headers=auth_headers, json={"visibility": "unlisted"}
    )
    assert resp.status_code == 400


async def test_patch_is_video_only(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    image = await fake_metadata.create(
        owner=OWNER,
        kind="image",
        storage_key="images/a.webp",
        content_type="image/webp",
        size_bytes=1,
        status="ready",
    )
    resp = await client.patch(
        f"/files/{image.id}", headers=auth_headers, json={"visibility": "public"}
    )
    assert resp.status_code == 400


async def test_patch_is_owner_scoped_404(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    other = await _seed_video(fake_metadata, owner="someone-else")
    resp = await client.patch(
        f"/files/{other.id}", headers=auth_headers, json={"visibility": "public"}
    )
    assert resp.status_code == 404


async def test_patch_requires_auth(
    client: httpx.AsyncClient, fake_metadata: InMemoryMetadataStore
) -> None:
    rec = await _seed_video(fake_metadata)
    resp = await client.patch(f"/files/{rec.id}", json={"visibility": "public"})
    assert resp.status_code == 401


# --- POST/DELETE /files/{id}/share ----------------------------------------


async def test_share_mint_returns_token_and_persists_it(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    rec = await _seed_video(fake_metadata)
    resp = await client.post(f"/files/{rec.id}/share", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    token = body["share_token"]
    assert token and body["share_url"].endswith(f"/share/{token}")
    # Persisted, and resolvable by the unscoped lookup.
    found = await fake_metadata.get_by_share_token(token)
    assert found is not None and found.id == rec.id


async def test_share_token_is_never_in_to_public(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    rec = await _seed_video(fake_metadata)
    await client.post(f"/files/{rec.id}/share", headers=auth_headers)
    # GET /files/{id} must not leak the secret token (only visibility is public).
    body = (await client.get(f"/files/{rec.id}", headers=auth_headers)).json()
    assert "share_token" not in body
    assert "visibility" in body
    # Nor in listings.
    listed = (await client.get("/files", headers=auth_headers)).json()["files"]
    assert all("share_token" not in f for f in listed)


async def test_share_mint_rotates_and_revokes_previous(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    rec = await _seed_video(fake_metadata)
    first = (await client.post(f"/files/{rec.id}/share", headers=auth_headers)).json()[
        "share_token"
    ]
    second = (await client.post(f"/files/{rec.id}/share", headers=auth_headers)).json()[
        "share_token"
    ]
    assert first != second
    assert await fake_metadata.get_by_share_token(first) is None  # old one revoked
    assert (await fake_metadata.get_by_share_token(second)).id == rec.id


async def test_share_revoke_clears_token(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    rec = await _seed_video(fake_metadata)
    token = (await client.post(f"/files/{rec.id}/share", headers=auth_headers)).json()[
        "share_token"
    ]
    resp = await client.delete(f"/files/{rec.id}/share", headers=auth_headers)
    assert resp.status_code == 204
    assert await fake_metadata.get_by_share_token(token) is None


async def test_share_is_owner_scoped_404(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    other = await _seed_video(fake_metadata, owner="someone-else")
    post = await client.post(f"/files/{other.id}/share", headers=auth_headers)
    delete = await client.delete(f"/files/{other.id}/share", headers=auth_headers)
    assert post.status_code == 404
    assert delete.status_code == 404


async def test_share_is_video_only(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    image = await fake_metadata.create(
        owner=OWNER,
        kind="image",
        storage_key="images/a.webp",
        content_type="image/webp",
        size_bytes=1,
        status="ready",
    )
    assert (await client.post(f"/files/{image.id}/share", headers=auth_headers)).status_code == 400


async def test_share_requires_auth(
    client: httpx.AsyncClient, fake_metadata: InMemoryMetadataStore
) -> None:
    rec = await _seed_video(fake_metadata)
    assert (await client.post(f"/files/{rec.id}/share")).status_code == 401
