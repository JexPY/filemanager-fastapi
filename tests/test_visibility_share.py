"""Owner visibility (PATCH /files/{id}) and share-link management
(POST/DELETE /files/{id}/share). Serving the bytes is covered in test_playback."""

from __future__ import annotations

import httpx

from app.config import _derive_owner
from app.services.metadata import UploadRecord
from tests.fakes import InMemoryMetadataStore, InMemoryStorageBackend

OWNER = _derive_owner("test-token")


async def _seed_video(
    store: InMemoryMetadataStore,
    owner: str = OWNER,
    key: str = "videos/v.mp4",
    *,
    visibility: str = "private",
) -> UploadRecord:
    return await store.create(
        owner=owner,
        kind="video",
        storage_key=key,
        content_type="video/mp4",
        size_bytes=1,
        status="ready",
        visibility=visibility,
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
    # The _VisibilityBody Literal rejects an out-of-enum value at validation time.
    assert resp.status_code == 422


async def test_patch_visibility_applies_to_any_kind(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    """Visibility is a property of a record, not of videos.

    Replaces a test asserting images were rejected with 400. Flipping an image
    to private must also withhold the accelerator URLs, since those bypass the
    app's auth entirely -- leaving them would make "private" cosmetic.
    """
    image = await fake_metadata.create(
        owner=OWNER,
        kind="image",
        storage_key="images/a.webp",
        content_type="image/webp",
        size_bytes=1,
        status="ready",
        visibility="public",
    )
    assert "thumbnail_url" in image.to_public()

    resp = await client.patch(
        f"/files/{image.id}", headers=auth_headers, json={"visibility": "private"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["visibility"] == "private"
    assert "thumbnail_url" not in body
    assert "direct_url" not in body
    assert body["url"].endswith(f"/files/{image.id}/download")


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
    assert token
    assert body["share_url"].endswith(f"/share/{token}")
    # Persisted, and resolvable by the unscoped lookup.
    found = await fake_metadata.get_by_share_token(token)
    assert found is not None
    assert found.id == rec.id


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


async def test_going_private_rotates_the_storage_key(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_storage: InMemoryStorageBackend,
) -> None:
    """Turning a record private must invalidate what is already cached, not just
    stop advertising it.

    While public, the object URL may have been fetched through a CDN and embedded
    in rendered HTML; neither can be recalled. Copying to a fresh key kills every
    such URL at once -- including all imgproxy renditions, since an imgproxy URL
    signs its source. Leaving the key alone would keep the old bytes served from
    a warm cache indefinitely, which would make "private" cosmetic.
    """
    await fake_storage.upload(b"secret-bytes", "images/a.webp", "image/webp")
    image = await fake_metadata.create(
        owner=OWNER,
        kind="image",
        storage_key="images/a.webp",
        content_type="image/webp",
        size_bytes=12,
        status="ready",
        visibility="public",
    )

    resp = await client.patch(
        f"/files/{image.id}", headers=auth_headers, json={"visibility": "private"}
    )
    assert resp.status_code == 200

    stored = await fake_metadata.get(image.id, OWNER)
    assert stored is not None
    assert stored.storage_key != "images/a.webp"
    assert stored.storage_key.startswith("images/")
    assert stored.storage_key.endswith(".webp")
    # Bytes moved, and the old object is gone so its cached URL now 404s.
    assert fake_storage.objects[stored.storage_key] == b"secret-bytes"
    assert "images/a.webp" not in fake_storage.objects
    assert "images/a.webp" in fake_storage.deleted_keys


async def test_going_public_does_not_rotate(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_storage: InMemoryStorageBackend,
) -> None:
    """The reverse direction has nothing cached to invalidate, so rotating would
    be a pointless copy of (potentially) a multi-hundred-MB video."""
    await fake_storage.upload(b"bytes", "images/b.webp", "image/webp")
    image = await fake_metadata.create(
        owner=OWNER,
        kind="image",
        storage_key="images/b.webp",
        content_type="image/webp",
        size_bytes=5,
        status="ready",
        visibility="private",
    )

    resp = await client.patch(
        f"/files/{image.id}", headers=auth_headers, json={"visibility": "public"}
    )

    assert resp.status_code == 200
    stored = await fake_metadata.get(image.id, OWNER)
    assert stored is not None
    assert stored.storage_key == "images/b.webp"
    assert fake_storage.deleted_keys == []


async def test_going_private_cascades_to_the_poster(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_storage: InMemoryStorageBackend,
) -> None:
    """A poster is its own record with its own key and its own public URLs, so a
    private video with a still-public poster leaks a thumbnail of itself."""
    await fake_storage.upload(b"vid", "videos/v.mp4", "video/mp4")
    await fake_storage.upload(b"post", "posters/p.webp", "image/webp")
    video = await _seed_video(fake_metadata, visibility="public", key="videos/v.mp4")
    poster = await fake_metadata.create(
        owner=OWNER,
        kind="image",
        storage_key="posters/p.webp",
        content_type="image/webp",
        size_bytes=4,
        status="ready",
        visibility="public",
    )
    await fake_metadata.set_poster(video.id, poster.id)

    resp = await client.patch(
        f"/files/{video.id}", headers=auth_headers, json={"visibility": "private"}
    )
    assert resp.status_code == 200

    stored_poster = await fake_metadata.get(poster.id, OWNER)
    assert stored_poster is not None
    assert stored_poster.visibility == "private"
    assert stored_poster.storage_key != "posters/p.webp"
    assert "posters/p.webp" not in fake_storage.objects


async def test_going_private_survives_a_missing_object(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    """A record whose object is already gone is reachable in production -- DELETE
    removes the object before the row, so a failed row-delete leaves one. The
    owner must still be able to make it private rather than being trapped on a
    502 by a rotation that has nothing to copy."""
    image = await fake_metadata.create(
        owner=OWNER,
        kind="image",
        storage_key="images/vanished.webp",
        content_type="image/webp",
        size_bytes=1,
        status="ready",
        visibility="public",
    )

    resp = await client.patch(
        f"/files/{image.id}", headers=auth_headers, json={"visibility": "private"}
    )

    assert resp.status_code == 200
    assert resp.json()["visibility"] == "private"


async def test_share_can_be_minted_for_any_kind(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    """Replaces a test asserting images were rejected with 400. Sharing a private
    image by link is exactly as meaningful as sharing a video, and the token
    still never appears in the record view."""
    image = await fake_metadata.create(
        owner=OWNER,
        kind="image",
        storage_key="images/a.webp",
        content_type="image/webp",
        size_bytes=1,
        status="ready",
        visibility="private",
    )

    resp = await client.post(f"/files/{image.id}/share", headers=auth_headers)

    assert resp.status_code == 200
    token = resp.json()["share_token"]
    assert token
    stored = await fake_metadata.get(image.id, OWNER)
    assert stored is not None
    assert "share_token" not in stored.to_public()


async def test_share_requires_auth(
    client: httpx.AsyncClient, fake_metadata: InMemoryMetadataStore
) -> None:
    rec = await _seed_video(fake_metadata)
    assert (await client.post(f"/files/{rec.id}/share")).status_code == 401
