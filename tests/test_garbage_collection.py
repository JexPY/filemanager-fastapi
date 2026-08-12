from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import _derive_owner
from app.main import app
from app.services.metadata import (
    KIND_IMAGE,
    STATUS_READY,
)
from app.tasks import cleanup_orphaned_uploads_task
from tests.fakes import InMemoryMetadataStore, InMemoryStorageBackend

OWNER = _derive_owner("test-token")


@pytest.mark.asyncio
async def test_in_memory_mark_linked_and_unlinked_older_than(
    fake_metadata: InMemoryMetadataStore,
) -> None:
    now = datetime.now(UTC)

    # 1. Create an unlinked record older than 24 hours (30h old)
    r1 = await fake_metadata.create(
        owner="owner-1",
        kind=KIND_IMAGE,
        storage_key="images/old-unlinked.webp",
        content_type="image/webp",
        size_bytes=100,
        status=STATUS_READY,
    )
    # Manually backdate created_at for r1 to 30 hours ago
    fake_metadata.records[r1.id] = r1.__class__(
        **{**r1.__dict__, "created_at": now - timedelta(hours=30)}
    )

    # 2. Create a recent unlinked record (5h old)
    r2 = await fake_metadata.create(
        owner="owner-1",
        kind=KIND_IMAGE,
        storage_key="images/recent-unlinked.webp",
        content_type="image/webp",
        size_bytes=100,
        status=STATUS_READY,
    )
    fake_metadata.records[r2.id] = r2.__class__(
        **{**r2.__dict__, "created_at": now - timedelta(hours=5)}
    )

    # 3. Create an old record that is linked (40h old)
    r3 = await fake_metadata.create(
        owner="owner-1",
        kind=KIND_IMAGE,
        storage_key="images/old-linked.webp",
        content_type="image/webp",
        size_bytes=100,
        status=STATUS_READY,
    )
    fake_metadata.records[r3.id] = r3.__class__(
        **{**r3.__dict__, "created_at": now - timedelta(hours=40)}
    )
    await fake_metadata.mark_linked(r3.id, "owner-1")

    # Fetch unlinked records older than 24h
    unlinked = await fake_metadata.get_unlinked_older_than(hours=24)
    assert len(unlinked) == 1
    assert unlinked[0].id == r1.id
    assert not unlinked[0].is_linked

    # Mark r1 as linked
    updated = await fake_metadata.mark_linked(r1.id, "owner-1")
    assert updated is not None
    assert updated.is_linked is True

    # Now get_unlinked_older_than should return empty list
    unlinked_after = await fake_metadata.get_unlinked_older_than(hours=24)
    assert len(unlinked_after) == 0


@pytest.mark.asyncio
async def test_patch_mark_file_linked_route(
    fake_metadata: InMemoryMetadataStore,
) -> None:
    rec = await fake_metadata.create(
        owner=OWNER,
        kind=KIND_IMAGE,
        storage_key="images/test.webp",
        content_type="image/webp",
        size_bytes=200,
        status=STATUS_READY,
    )
    assert rec.is_linked is False

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Unauthenticated request fails (401)
        res_401 = await client.patch(f"/files/{rec.id}/link")
        assert res_401.status_code == 401

        # 2. Authenticated request marks file as linked (200)
        res_200 = await client.patch(
            f"/files/{rec.id}/link",
            headers={"Authorization": "Bearer test-token"},
        )
        assert res_200.status_code == 200
        data = res_200.json()
        assert data["id"] == rec.id
        assert data["is_linked"] is True

        # 3. Non-existent or other owner's file returns 404
        res_404 = await client.patch(
            "/files/nonexistent/link",
            headers={"Authorization": "Bearer test-token"},
        )
        assert res_404.status_code == 404


@pytest.mark.asyncio
async def test_cleanup_orphaned_uploads_task(
    fake_metadata: InMemoryMetadataStore,
    fake_storage: InMemoryStorageBackend,
) -> None:
    now = datetime.now(UTC)

    # Setup 1: Old unlinked file with valid storage object (should be deleted)
    obj1 = await fake_storage.upload(b"data1", "images/old1.webp", "image/webp")
    rec1 = await fake_metadata.create(
        owner="user-a",
        kind=KIND_IMAGE,
        storage_key=obj1.key,
        content_type="image/webp",
        size_bytes=5,
        status=STATUS_READY,
    )
    fake_metadata.records[rec1.id] = rec1.__class__(
        **{**rec1.__dict__, "created_at": now - timedelta(hours=36)}
    )

    # Setup 2: Old unlinked file whose physical storage is ALREADY MISSING (deletes row)
    rec2 = await fake_metadata.create(
        owner="user-a",
        kind=KIND_IMAGE,
        storage_key="images/already-missing.webp",
        content_type="image/webp",
        size_bytes=5,
        status=STATUS_READY,
    )
    fake_metadata.records[rec2.id] = rec2.__class__(
        **{**rec2.__dict__, "created_at": now - timedelta(hours=48)}
    )

    # Setup 3: Recent unlinked file (should be preserved)
    obj3 = await fake_storage.upload(b"data3", "images/recent3.webp", "image/webp")
    rec3 = await fake_metadata.create(
        owner="user-a",
        kind=KIND_IMAGE,
        storage_key=obj3.key,
        content_type="image/webp",
        size_bytes=5,
        status=STATUS_READY,
    )
    fake_metadata.records[rec3.id] = rec3.__class__(
        **{**rec3.__dict__, "created_at": now - timedelta(hours=2)}
    )

    # Setup 4: Old LINKED file (should be preserved)
    obj4 = await fake_storage.upload(b"data4", "images/old4-linked.webp", "image/webp")
    rec4 = await fake_metadata.create(
        owner="user-a",
        kind=KIND_IMAGE,
        storage_key=obj4.key,
        content_type="image/webp",
        size_bytes=5,
        status=STATUS_READY,
    )
    fake_metadata.records[rec4.id] = rec4.__class__(
        **{**rec4.__dict__, "created_at": now - timedelta(hours=50)}
    )
    await fake_metadata.mark_linked(rec4.id, "user-a")

    # Execute cleanup task
    result = await cleanup_orphaned_uploads_task(hours=24, batch_size=10)

    assert result["status"] == "success"
    assert result["deleted"] == 2  # rec1 and rec2
    assert result["failed"] == 0

    # Verify rec1 and rec2 are gone from metadata store
    assert await fake_metadata.get_by_id(rec1.id) is None
    assert await fake_metadata.get_by_id(rec2.id) is None

    # Verify rec1 storage object was deleted
    assert "images/old1.webp" in fake_storage.deleted_keys

    # Verify rec3 (recent) and rec4 (linked) are still intact
    assert await fake_metadata.get_by_id(rec3.id) is not None
    assert await fake_metadata.get_by_id(rec4.id) is not None
