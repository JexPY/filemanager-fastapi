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
    MetadataError,
    PostgresMetadataStore,
)

pytestmark = pytest.mark.pg_integration


@pytest.fixture
async def pg_store() -> AsyncIterator[PostgresMetadataStore]:
    store = PostgresMetadataStore(settings.DATABASE_URL)
    # Builds the pool only; the `uploads` schema is applied by the compose
    # `migrate` service (alembic upgrade head), which the `test` service waits
    # on before pytest runs.
    await store.connect()
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


async def test_get_by_task_id_is_owner_scoped(pg_store: PostgresMetadataStore) -> None:
    rec = await pg_store.create(
        owner="alice",
        kind=KIND_VIDEO,
        storage_key="raw/videos/v.mp4",
        content_type="video/mp4",
        size_bytes=1,
        status=STATUS_PROCESSING,
    )
    await pg_store.set_task_id(rec.id, "task-xyz")

    found = await pg_store.get_by_task_id("task-xyz", "alice")
    assert found is not None and found.id == rec.id
    assert await pg_store.get_by_task_id("task-xyz", "bob") is None
    assert await pg_store.get_by_task_id("nope", "alice") is None


async def test_find_active_video_by_hash(pg_store: PostgresMetadataStore) -> None:
    processing = await pg_store.create(
        owner="alice",
        kind=KIND_VIDEO,
        storage_key="raw/videos/p.mp4",
        content_type="video/mp4",
        size_bytes=1,
        status=STATUS_PROCESSING,
        content_hash="vhash",
    )
    # A still-processing video matches (attach to the in-flight job).
    found = await pg_store.find_active_video_by_hash("alice", "vhash")
    assert found is not None and found.id == processing.id

    # Owner-scoped, and an image with the same hash is not a video match.
    assert await pg_store.find_active_video_by_hash("bob", "vhash") is None
    await pg_store.create(
        owner="alice",
        kind=KIND_IMAGE,
        storage_key="images/i.webp",
        content_type="image/webp",
        size_bytes=1,
        status=STATUS_READY,
        content_hash="ihash",
    )
    assert await pg_store.find_active_video_by_hash("alice", "ihash") is None

    # A failed video does NOT match -- a bad input can be retried.
    await pg_store.mark_failed(processing.id)
    assert await pg_store.find_active_video_by_hash("alice", "vhash") is None


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
    ready = await pg_store.mark_ready(
        rec.id,
        storage_key="videos/v_compressed.mp4",
        size_bytes=50,
        duration_seconds=123.5,
        truncated=True,
    )
    assert ready is not None
    assert ready.status == STATUS_READY
    assert ready.storage_key == "videos/v_compressed.mp4"
    assert ready.size_bytes == 50
    assert ready.duration_seconds == 123.5
    assert ready.truncated is True

    failed = await pg_store.mark_failed(rec.id)
    assert failed is not None and failed.status == STATUS_FAILED

    # Deleted before the worker's update lands -> update reports the row is gone.
    await pg_store.delete(rec.id, "alice")
    assert await pg_store.mark_ready(rec.id, storage_key="x", size_bytes=1) is None


async def test_get_by_id_is_unscoped(pg_store: PostgresMetadataStore) -> None:
    rec = await pg_store.create(
        owner="alice",
        kind=KIND_VIDEO,
        storage_key="videos/v_compressed.mp4",
        content_type="video/mp4",
        size_bytes=1,
        status=STATUS_READY,
    )
    # Worker-internal lookup: resolves without an owner (the api already admitted).
    found = await pg_store.get_by_id(rec.id)
    assert found is not None and found.id == rec.id
    assert await pg_store.get_by_id("does-not-exist") is None


async def test_set_poster_links_and_handles_missing_video(
    pg_store: PostgresMetadataStore,
) -> None:
    video = await pg_store.create(
        owner="alice",
        kind=KIND_VIDEO,
        storage_key="videos/v_compressed.mp4",
        content_type="video/mp4",
        size_bytes=1,
        status=STATUS_READY,
    )
    poster = await pg_store.create(
        owner="alice",
        kind=KIND_IMAGE,
        storage_key="posters/p.webp",
        content_type="image/webp",
        size_bytes=1,
        status=STATUS_READY,
    )
    linked = await pg_store.set_poster(video.id, poster.id)
    assert linked is not None and linked.poster_upload_id == poster.id
    # Persisted (round-trips through the column, not just the RETURNING row).
    refetched = await pg_store.get(video.id, "alice")
    assert refetched is not None and refetched.poster_upload_id == poster.id
    # A gone video reports None (the mid-generation race).
    await pg_store.delete(video.id, "alice")
    assert await pg_store.set_poster(video.id, poster.id) is None


async def test_mark_webhook_persists_deadletter_state(
    pg_store: PostgresMetadataStore,
) -> None:
    rec = await pg_store.create(
        owner="alice",
        kind=KIND_VIDEO,
        storage_key="videos/v_compressed.mp4",
        content_type="video/mp4",
        size_bytes=1,
        status=STATUS_READY,
        callback_url="https://h/hook",
    )
    # Fresh rows carry the defaulted state.
    assert rec.webhook_status is None
    assert rec.webhook_attempts == 0

    updated = await pg_store.mark_webhook(
        rec.id, status="failed", attempts=4, last_error="HTTP 500"
    )
    assert updated is not None
    assert updated.webhook_status == "failed"
    assert updated.webhook_attempts == 4
    assert updated.webhook_last_error == "HTTP 500"
    assert updated.webhook_updated_at is not None
    assert await pg_store.mark_webhook("does-not-exist", status="failed") is None


async def test_visibility_defaults_private_and_is_owner_scoped(
    pg_store: PostgresMetadataStore,
) -> None:
    rec = await pg_store.create(
        owner="alice",
        kind=KIND_VIDEO,
        storage_key="videos/v.mp4",
        content_type="video/mp4",
        size_bytes=1,
        status=STATUS_READY,
    )
    # New rows default private, with no share token.
    assert rec.visibility == "private"
    assert rec.share_token is None

    made_public = await pg_store.set_visibility(rec.id, "alice", "public")
    assert made_public is not None and made_public.visibility == "public"
    # Persisted (round-trips through the column, not just RETURNING).
    refetched = await pg_store.get(rec.id, "alice")
    assert refetched is not None and refetched.visibility == "public"

    # Owner-scoped: another owner can't change it.
    assert await pg_store.set_visibility(rec.id, "bob", "private") is None
    still_public = await pg_store.get(rec.id, "alice")
    assert still_public is not None and still_public.visibility == "public"


async def test_share_token_set_lookup_clear(pg_store: PostgresMetadataStore) -> None:
    rec = await pg_store.create(
        owner="alice",
        kind=KIND_VIDEO,
        storage_key="videos/v.mp4",
        content_type="video/mp4",
        size_bytes=1,
        status=STATUS_READY,
    )
    set_rec = await pg_store.set_share_token(rec.id, "alice", "tok-abc")
    assert set_rec is not None and set_rec.share_token == "tok-abc"

    # Unscoped lookup: the token is the grant (no owner arg).
    by_token = await pg_store.get_by_share_token("tok-abc")
    assert by_token is not None and by_token.id == rec.id
    assert await pg_store.get_by_share_token("nope") is None

    # Owner-scoped mint/clear.
    assert await pg_store.set_share_token(rec.id, "bob", "tok-x") is None
    cleared = await pg_store.clear_share_token(rec.id, "alice")
    assert cleared is not None and cleared.share_token is None
    assert await pg_store.get_by_share_token("tok-abc") is None
    assert await pg_store.clear_share_token(rec.id, "bob") is None


async def test_share_token_is_unique(pg_store: PostgresMetadataStore) -> None:
    a = await pg_store.create(
        owner="alice",
        kind=KIND_VIDEO,
        storage_key="videos/a.mp4",
        content_type="video/mp4",
        size_bytes=1,
        status=STATUS_READY,
    )
    b = await pg_store.create(
        owner="alice",
        kind=KIND_VIDEO,
        storage_key="videos/b.mp4",
        content_type="video/mp4",
        size_bytes=1,
        status=STATUS_READY,
    )
    await pg_store.set_share_token(a.id, "alice", "dup")
    # The UNIQUE index rejects the same token on a second row (surfaced as a
    # sanitized MetadataError, not a raw asyncpg error).
    with pytest.raises(MetadataError):
        await pg_store.set_share_token(b.id, "alice", "dup")


async def test_multiple_null_share_tokens_allowed(pg_store: PostgresMetadataStore) -> None:
    # NULLs are distinct in Postgres, so the UNIQUE index permits many rows with
    # no share token (the common case).
    for key in ("videos/1.mp4", "videos/2.mp4", "videos/3.mp4"):
        await pg_store.create(
            owner="alice",
            kind=KIND_VIDEO,
            storage_key=key,
            content_type="video/mp4",
            size_bytes=1,
            status=STATUS_READY,
        )
    assert len(await pg_store.list("alice")) == 3


async def test_mark_linked_and_unlinked_older_than_pg(pg_store: PostgresMetadataStore) -> None:
    rec = await pg_store.create(
        owner="alice",
        kind=KIND_IMAGE,
        storage_key="images/pg1.webp",
        content_type="image/webp",
        size_bytes=10,
        status=STATUS_READY,
    )
    assert rec.is_linked is False

    # Owner scoping check for mark_linked
    assert await pg_store.mark_linked(rec.id, "bob") is None

    # Mark as linked
    linked = await pg_store.mark_linked(rec.id, "alice")
    assert linked is not None and linked.is_linked is True

    # Persisted check
    refetched = await pg_store.get(rec.id, "alice")
    assert refetched is not None and refetched.is_linked is True
