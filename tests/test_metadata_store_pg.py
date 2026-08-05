"""Integration tests for PostgresMetadataStore against the live `db` service.

Marked `pg_integration`; they run by default because the compose `test` service
depends on `db` (deselect with `-m "not pg_integration"` when running without
it). This is what actually verifies the SQL, the schema DDL, and asyncpg's type
mapping -- the fake can't. Each test truncates the shared table first.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest

from app.config import settings
from app.services.metadata import (
    KIND_IMAGE,
    KIND_VIDEO,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_READY,
    PostgresMetadataStore,
)

pytestmark = pytest.mark.pg_integration


@pytest.fixture
async def pg_store() -> AsyncIterator[PostgresMetadataStore]:
    store = PostgresMetadataStore(settings.DATABASE_URL)
    await store.connect()  # builds the pool and ensures the schema
    conn = await asyncpg.connect(settings.DATABASE_URL)
    try:
        await conn.execute("TRUNCATE uploads")
    finally:
        await conn.close()
    try:
        yield store
    finally:
        await store.aclose()


async def test_full_crud_roundtrip(pg_store: PostgresMetadataStore) -> None:
    rec = await pg_store.create(
        owner="alice",
        kind=KIND_IMAGE,
        storage_key="images/a.webp",
        content_type="image/webp",
        size_bytes=42,
        status=STATUS_READY,
        width=640,
        height=480,
        content_hash="abc123",
    )
    assert rec.id and rec.created_at is not None

    fetched = await pg_store.get(rec.id, "alice")
    assert fetched is not None and fetched.storage_key == "images/a.webp"
    assert fetched.content_hash == "abc123"
    assert (fetched.width, fetched.height) == (640, 480)

    listed = await pg_store.list("alice")
    assert [r.id for r in listed] == [rec.id]

    deleted = await pg_store.delete(rec.id, "alice")
    assert deleted is not None
    assert await pg_store.get(rec.id, "alice") is None


async def test_owner_scoping(pg_store: PostgresMetadataStore) -> None:
    rec = await pg_store.create(
        owner="alice",
        kind=KIND_IMAGE,
        storage_key="images/a.webp",
        content_type="image/webp",
        size_bytes=1,
        status=STATUS_READY,
    )
    assert await pg_store.get(rec.id, "bob") is None
    assert await pg_store.delete(rec.id, "bob") is None
    assert await pg_store.list("bob") == []
    # Still present for its real owner after the cross-owner attempts.
    assert await pg_store.get(rec.id, "alice") is not None


async def test_list_ordering_pagination_and_kind_filter(pg_store: PostgresMetadataStore) -> None:
    first = await pg_store.create(
        owner="alice",
        kind=KIND_IMAGE,
        storage_key="images/1.webp",
        content_type="image/webp",
        size_bytes=1,
        status=STATUS_READY,
    )
    second = await pg_store.create(
        owner="alice",
        kind=KIND_IMAGE,
        storage_key="images/2.webp",
        content_type="image/webp",
        size_bytes=1,
        status=STATUS_READY,
    )
    video = await pg_store.create(
        owner="alice",
        kind=KIND_VIDEO,
        storage_key="raw/videos/v.mp4",
        content_type="video/mp4",
        size_bytes=1,
        status=STATUS_PROCESSING,
    )

    ids = [r.id for r in await pg_store.list("alice")]
    assert ids == [video.id, second.id, first.id]  # newest first
    assert len(await pg_store.list("alice", limit=2)) == 2
    assert [r.id for r in await pg_store.list("alice", limit=2, offset=2)] == [first.id]
    assert [r.kind for r in await pg_store.list("alice", kind=KIND_VIDEO)] == [KIND_VIDEO]


async def test_find_ready_by_hash(pg_store: PostgresMetadataStore) -> None:
    rec = await pg_store.create(
        owner="alice",
        kind=KIND_IMAGE,
        storage_key="images/a.webp",
        content_type="image/webp",
        size_bytes=1,
        status=STATUS_READY,
        content_hash="hash-1",
    )
    found = await pg_store.find_ready_by_hash("alice", "hash-1")
    assert found is not None and found.id == rec.id
    assert await pg_store.find_ready_by_hash("bob", "hash-1") is None
    assert await pg_store.find_ready_by_hash("alice", "missing") is None


async def test_video_lifecycle_and_deleted_midflight(pg_store: PostgresMetadataStore) -> None:
    rec = await pg_store.create(
        owner="alice",
        kind=KIND_VIDEO,
        storage_key="raw/videos/v.mp4",
        content_type="video/mp4",
        size_bytes=100,
        status=STATUS_PROCESSING,
        task_id="task-1",
    )
    ready = await pg_store.mark_ready(rec.id, storage_key="videos/v_compressed.mp4", size_bytes=50)
    assert ready is not None
    assert ready.status == STATUS_READY
    assert ready.storage_key == "videos/v_compressed.mp4"
    assert ready.size_bytes == 50

    failed = await pg_store.mark_failed(rec.id)
    assert failed is not None and failed.status == STATUS_FAILED

    # Deleted before the worker's update lands -> update reports the row is gone.
    await pg_store.delete(rec.id, "alice")
    assert await pg_store.mark_ready(rec.id, storage_key="x", size_bytes=1) is None
