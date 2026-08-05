"""Every upload lands a metadata record, owned by the calling token."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.config import _derive_owner, settings
from tests.conftest import fixture_bytes
from tests.fakes import InMemoryMetadataStore

# The auth_headers fixture configures this single bare token.
TEST_OWNER = _derive_owner("test-token")


async def test_image_upload_creates_a_ready_record(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    resp = await client.post(
        "/upload/image",
        headers=auth_headers,
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    assert resp.status_code == 200
    body = resp.json()

    records = await fake_metadata.list(TEST_OWNER)
    assert len(records) == 1
    record = records[0]
    assert record.id == body["id"]
    assert record.owner == TEST_OWNER
    assert record.kind == "image"
    assert record.status == "ready"
    assert record.storage_key.startswith("images/")
    assert record.content_type == "image/webp"


async def test_video_upload_creates_a_processing_record_with_task_id(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_enqueue: list[dict[str, Any]],
) -> None:
    resp = await client.post(
        "/upload/video",
        headers=auth_headers,
        files={"file": ("tiny.mp4", fixture_bytes("tiny.mp4"), "video/mp4")},
    )
    assert resp.status_code == 202
    body = resp.json()

    record = await fake_metadata.get(body["id"], TEST_OWNER)
    assert record is not None
    assert record.kind == "video"
    assert record.status == "processing"
    assert record.storage_key.startswith("raw/videos/")
    assert record.original_filename == "tiny.mp4"
    # set_task_id ran after enqueue, linking the record to its compression task.
    assert record.task_id == body["task_id"]

    # The worker was handed the record id so it can mark it ready later.
    assert fake_enqueue[0]["upload_id"] == body["id"]


async def test_uploads_are_scoped_to_the_calling_token(
    client: httpx.AsyncClient,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "FILE_MANAGER_BEARER_TOKENS", "alice:tok-a,bob:tok-b")
    png = fixture_bytes("tiny.png")

    await client.post(
        "/upload/image",
        headers={"Authorization": "Bearer tok-a"},
        files={"file": ("a.png", png, "image/png")},
    )
    await client.post(
        "/upload/image",
        headers={"Authorization": "Bearer tok-b"},
        files={"file": ("b.png", png, "image/png")},
    )

    # Each token sees exactly its own upload -- the per-token audit trail.
    assert len(await fake_metadata.list("alice")) == 1
    assert len(await fake_metadata.list("bob")) == 1
