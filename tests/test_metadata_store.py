"""Contract tests for the metadata store, exercised against the in-memory fake.

The routes are tested through this same fake, so locking its observable
behavior (owner scoping, list ordering/pagination, the delete-during-processing
race) is what makes those route tests trustworthy. The identical contract is
verified against real Postgres in test_metadata_store_pg.py.
"""

from __future__ import annotations

from app.services.metadata import (
    KIND_IMAGE,
    KIND_VIDEO,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_READY,
)
from tests.fakes import InMemoryMetadataStore


async def _seed_image(store: InMemoryMetadataStore, owner: str, key: str, **kw: object):
    return await store.create(
        owner=owner,
        kind=KIND_IMAGE,
        storage_key=key,
        content_type="image/webp",
        size_bytes=123,
        status=STATUS_READY,
        **kw,
    )


async def test_create_and_get_roundtrip() -> None:
    store = InMemoryMetadataStore()
    rec = await _seed_image(store, "alice", "images/a.webp", content_hash="deadbeef")
    fetched = await store.get(rec.id, "alice")
    assert fetched == rec
    assert fetched.status == STATUS_READY


async def test_get_is_owner_scoped() -> None:
    store = InMemoryMetadataStore()
    rec = await _seed_image(store, "alice", "images/a.webp")
    # Another owner must not be able to fetch it -- and it 404s as "absent",
    # not "forbidden", so existence never leaks across tenants.
    assert await store.get(rec.id, "bob") is None


async def test_list_is_owner_scoped_and_newest_first() -> None:
    store = InMemoryMetadataStore()
    first = await _seed_image(store, "alice", "images/1.webp")
    second = await _seed_image(store, "alice", "images/2.webp")
    await _seed_image(store, "bob", "images/3.webp")

    listed = await store.list("alice")
    assert [r.id for r in listed] == [second.id, first.id]  # newest first
    assert all(r.owner == "alice" for r in listed)


async def test_list_pagination_and_kind_filter() -> None:
    store = InMemoryMetadataStore()
    await _seed_image(store, "alice", "images/1.webp")
    await _seed_image(store, "alice", "images/2.webp")
    await store.create(
        owner="alice",
        kind=KIND_VIDEO,
        storage_key="raw/videos/v.mp4",
        content_type="video/mp4",
        size_bytes=999,
        status=STATUS_PROCESSING,
    )

    assert len(await store.list("alice", limit=2)) == 2
    assert len(await store.list("alice", limit=2, offset=2)) == 1
    videos = await store.list("alice", kind=KIND_VIDEO)
    assert [r.kind for r in videos] == [KIND_VIDEO]


async def test_get_many_is_owner_scoped_and_silently_omits_the_rest() -> None:
    store = InMemoryMetadataStore()
    a = await _seed_image(store, "alice", "images/a.webp")
    b = await _seed_image(store, "alice", "images/b.webp")
    bobs = await _seed_image(store, "bob", "images/c.webp")

    result = await store.get_many("alice", [a.id, b.id, bobs.id, "does-not-exist"])

    # Unknown id and another owner's id are silently absent, not an error.
    assert {r.id for r in result} == {a.id, b.id}


async def test_get_many_with_empty_ids_returns_empty_list() -> None:
    store = InMemoryMetadataStore()
    assert await store.get_many("alice", []) == []


async def test_delete_is_owner_scoped() -> None:
    store = InMemoryMetadataStore()
    rec = await _seed_image(store, "alice", "images/a.webp")

    assert await store.delete(rec.id, "bob") is None  # not bob's to delete
    assert await store.get(rec.id, "alice") is not None  # still there

    deleted = await store.delete(rec.id, "alice")
    assert deleted is not None
    assert deleted.storage_key == "images/a.webp"
    assert await store.get(rec.id, "alice") is None


async def test_find_ready_by_hash_scoped_to_owner_and_ready() -> None:
    store = InMemoryMetadataStore()
    rec = await _seed_image(store, "alice", "images/a.webp", content_hash="abc123")

    assert (await store.find_ready_by_hash("alice", "abc123")).id == rec.id
    assert await store.find_ready_by_hash("bob", "abc123") is None
    assert await store.find_ready_by_hash("alice", "nope") is None


async def test_mark_ready_transitions_processing_row() -> None:
    store = InMemoryMetadataStore()
    rec = await store.create(
        owner="alice",
        kind=KIND_VIDEO,
        storage_key="raw/videos/v.mp4",
        content_type="video/mp4",
        size_bytes=10,
        status=STATUS_PROCESSING,
        task_id="task-1",
    )
    updated = await store.mark_ready(rec.id, storage_key="videos/v_compressed.mp4", size_bytes=5)
    assert updated is not None
    assert updated.status == STATUS_READY
    assert updated.storage_key == "videos/v_compressed.mp4"
    assert updated.size_bytes == 5


async def test_mark_ready_returns_none_when_row_deleted_midflight() -> None:
    # The delete-during-processing race: the worker finishes compressing after
    # the owner already DELETEd the upload. mark_ready must report the row is
    # gone so the worker can clean up the object it just wrote instead of
    # leaving an orphan.
    store = InMemoryMetadataStore()
    rec = await store.create(
        owner="alice",
        kind=KIND_VIDEO,
        storage_key="raw/videos/v.mp4",
        content_type="video/mp4",
        size_bytes=10,
        status=STATUS_PROCESSING,
        task_id="task-1",
    )
    await store.delete(rec.id, "alice")
    assert await store.mark_ready(rec.id, storage_key="videos/v.mp4", size_bytes=5) is None


async def test_mark_failed_transitions_status() -> None:
    store = InMemoryMetadataStore()
    rec = await store.create(
        owner="alice",
        kind=KIND_VIDEO,
        storage_key="raw/videos/v.mp4",
        content_type="video/mp4",
        size_bytes=10,
        status=STATUS_PROCESSING,
    )
    updated = await store.mark_failed(rec.id)
    assert updated is not None
    assert updated.status == STATUS_FAILED


async def test_placeholders_persistence_roundtrip() -> None:
    store = InMemoryMetadataStore()
    rec = await store.create(
        owner="alice",
        kind=KIND_IMAGE,
        storage_key="images/ph.webp",
        content_type="image/webp",
        size_bytes=100,
        status=STATUS_READY,
        dominant_color="#1e293b",
        blur_data_url="data:image/webp;base64,UklGRl...",
    )
    assert rec.dominant_color == "#1e293b"
    assert rec.blur_data_url == "data:image/webp;base64,UklGRl..."

    fetched = await store.get(rec.id, "alice")
    assert fetched is not None
    assert fetched.dominant_color == "#1e293b"
    assert fetched.blur_data_url == "data:image/webp;base64,UklGRl..."


async def test_placeholders_null_for_unprovided() -> None:
    store = InMemoryMetadataStore()
    rec = await store.create(
        owner="alice",
        kind=KIND_IMAGE,
        storage_key="images/noph.webp",
        content_type="image/webp",
        size_bytes=100,
        status=STATUS_READY,
    )
    assert rec.dominant_color is None
    assert rec.blur_data_url is None

    fetched = await store.get(rec.id, "alice")
    assert fetched is not None
    assert fetched.dominant_color is None
    assert fetched.blur_data_url is None
