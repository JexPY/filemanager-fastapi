"""GET /files and GET /files/{id} -- owner-scoped listing and fetch."""

from __future__ import annotations

import httpx

from app.config import _derive_owner
from app.services.metadata import UploadRecord
from tests.fakes import InMemoryMetadataStore

OWNER = _derive_owner("test-token")


async def _seed(store: InMemoryMetadataStore, owner: str, kind: str, key: str) -> UploadRecord:
    return await store.create(
        owner=owner,
        kind=kind,
        storage_key=key,
        content_type="image/webp",
        size_bytes=1,
        status="ready",
    )


async def test_list_returns_only_callers_records_newest_first(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    first = await _seed(fake_metadata, OWNER, "image", "images/a.webp")
    second = await _seed(fake_metadata, OWNER, "image", "images/b.webp")
    await _seed(fake_metadata, "someone-else", "image", "images/c.webp")

    resp = await client.get("/files", headers=auth_headers)
    assert resp.status_code == 200
    files = resp.json()["files"]
    assert [f["id"] for f in files] == [second.id, first.id]  # newest first, no leakage
    assert files[0]["storage_key"] == "images/b.webp"
    assert "thumbnail_url" in files[0]
    assert "/rs:fill:300:300:0/g:no/" in files[0]["thumbnail_url"]


async def test_list_pagination_and_kind_filter(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    await _seed(fake_metadata, OWNER, "image", "images/1.webp")
    await _seed(fake_metadata, OWNER, "image", "images/2.webp")
    await _seed(fake_metadata, OWNER, "video", "raw/videos/v.mp4")

    assert len((await client.get("/files?limit=2", headers=auth_headers)).json()["files"]) == 2
    page2 = (await client.get("/files?limit=2&offset=2", headers=auth_headers)).json()["files"]
    assert len(page2) == 1
    videos = (await client.get("/files?kind=video", headers=auth_headers)).json()["files"]
    assert [f["kind"] for f in videos] == ["video"]


async def test_list_reports_total_count_independent_of_pagination(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    # Three of the caller's records (2 image, 1 video) plus one other owner's.
    await _seed(fake_metadata, OWNER, "image", "images/1.webp")
    await _seed(fake_metadata, OWNER, "image", "images/2.webp")
    await _seed(fake_metadata, OWNER, "video", "raw/videos/v.mp4")
    await _seed(fake_metadata, "someone-else", "image", "images/z.webp")

    # total_count is the owner's full count, not the (paginated) page size.
    body = (await client.get("/files?limit=1", headers=auth_headers)).json()
    assert len(body["files"]) == 1
    assert body["total_count"] == 3
    assert body["limit"] == 1 and body["offset"] == 0

    # It honors the kind filter, and never leaks the other owner's record.
    videos = (await client.get("/files?kind=video", headers=auth_headers)).json()
    assert videos["total_count"] == 1


async def test_list_rejects_out_of_range_limit(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    assert (await client.get("/files?limit=0", headers=auth_headers)).status_code == 422
    assert (await client.get("/files?limit=999", headers=auth_headers)).status_code == 422


async def test_list_requires_auth(client: httpx.AsyncClient) -> None:
    assert (await client.get("/files")).status_code == 401


async def test_get_returns_the_record(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    record = await _seed(fake_metadata, OWNER, "image", "images/a.webp")
    resp = await client.get(f"/files/{record.id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == record.id
    assert body["storage_key"] == "images/a.webp"
    assert body["status"] == "ready"
    assert "thumbnail_url" in body
    assert "/rs:fill:300:300:0/g:no/" in body["thumbnail_url"]


async def test_get_is_owner_scoped_404(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    other = await _seed(fake_metadata, "someone-else", "image", "images/x.webp")
    resp = await client.get(f"/files/{other.id}", headers=auth_headers)
    assert resp.status_code == 404


async def test_get_unknown_id_404(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    assert (await client.get("/files/nope", headers=auth_headers)).status_code == 404


async def test_get_requires_auth(
    client: httpx.AsyncClient, fake_metadata: InMemoryMetadataStore
) -> None:
    record = await _seed(fake_metadata, OWNER, "image", "images/a.webp")
    assert (await client.get(f"/files/{record.id}")).status_code == 401
