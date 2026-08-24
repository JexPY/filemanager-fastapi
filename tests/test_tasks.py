"""Tests for worker tasks, including video compression with automated poster extraction."""

from typing import Any

import pytest

import app.tasks as tasks_module
from app.services.metadata import KIND_IMAGE, KIND_VIDEO, STATUS_PROCESSING, STATUS_READY
from app.tasks import compress_video_task
from tests.conftest import fixture_bytes
from tests.fakes import InMemoryMetadataStore, InMemoryStorageBackend

OWNER = "alice"


async def _seed_video_processing(
    store: InMemoryMetadataStore, raw_key: str, callback_url: str | None = None
) -> str:
    record = await store.create(
        owner=OWNER,
        kind=KIND_VIDEO,
        storage_key=raw_key,
        content_type="video/mp4",
        size_bytes=1,
        status=STATUS_PROCESSING,
        callback_url=callback_url,
    )
    return record.id


async def test_compress_video_task_automated_poster_extraction(
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_key = "raw/videos/test_poster_auto.mp4"
    fake_storage.objects[raw_key] = fixture_bytes("tiny.mp4")
    upload_id = await _seed_video_processing(
        fake_metadata, raw_key, callback_url="https://example.com/webhook"
    )

    webhook_calls: list[dict[str, Any]] = []

    class _FakeWebhookTask:
        task_id = "webhook-task-1"

    async def fake_kiq(**kwargs: Any) -> _FakeWebhookTask:
        webhook_calls.append(kwargs)
        return _FakeWebhookTask()

    monkeypatch.setattr(tasks_module.deliver_webhook_task, "kiq", fake_kiq)

    result = await compress_video_task(
        raw_storage_key=raw_key,
        original_filename="tiny.mp4",
        upload_id=upload_id,
        poster_seconds=0.5,
    )

    assert result["status"] == "success"
    assert result["upload_id"] == upload_id
    assert "poster_id" in result

    poster_id = result["poster_id"]
    poster = await fake_metadata.get(poster_id, OWNER)
    assert poster is not None
    assert poster.kind == KIND_IMAGE
    assert poster.content_type == "image/webp"
    assert poster.storage_key.startswith("private/posters/")
    assert poster.storage_key in fake_storage.objects

    # Check video record linked to poster
    video = await fake_metadata.get(upload_id, OWNER)
    assert video is not None
    assert video.status == STATUS_READY
    assert video.poster_upload_id == poster_id

    # Webhook was enqueued with video record containing poster information
    assert len(webhook_calls) == 1
    assert webhook_calls[0]["upload_id"] == upload_id
    assert webhook_calls[0]["event"] == "video.completed"
