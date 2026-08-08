from typing import Any

import pytest

import app.tasks as tasks_module
from app.config import settings
from app.services.metadata import (
    KIND_VIDEO,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_READY,
)
from app.tasks import compress_video_task
from tests.conftest import fixture_bytes
from tests.fakes import InMemoryMetadataStore, InMemoryStorageBackend

OWNER = "alice"


async def _seed_processing(
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


@pytest.fixture
def captured_webhooks(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture deliver_webhook calls instead of making a real HTTP request."""
    calls: list[dict[str, Any]] = []

    async def fake_deliver(**kwargs: Any) -> bool:
        calls.append(kwargs)
        return True

    monkeypatch.setattr(tasks_module, "deliver_webhook", fake_deliver)
    return calls


async def test_successful_compression_uploads_output_marks_ready_and_deletes_raw(
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
) -> None:
    raw_key = "raw/videos/test123.mp4"
    fake_storage.objects[raw_key] = fixture_bytes("tiny.mp4")
    upload_id = await _seed_processing(fake_metadata, raw_key)

    result = await compress_video_task(
        raw_storage_key=raw_key, original_filename="tiny.mp4", upload_id=upload_id
    )

    assert result["status"] == "success"
    assert result["key"].startswith("videos/")
    assert result["key"] in fake_storage.objects
    assert result["upload_id"] == upload_id

    # tiny.mp4 is 2s, well under the 60s default cap -> not truncated, and the
    # probed input duration is reported.
    assert result["truncated"] is False
    assert result["max_duration_seconds"] == settings.VIDEO_MAX_DURATION_SECONDS
    assert result["duration_seconds"] == pytest.approx(2.0, abs=0.5)

    # The record is flipped to ready and now points at the compressed object.
    record = await fake_metadata.get(upload_id, OWNER)
    assert record is not None
    assert record.status == STATUS_READY
    assert record.storage_key == result["key"]
    assert record.truncated is False
    assert record.duration_seconds == pytest.approx(2.0, abs=0.5)

    # The raw upload is fully consumed by ffmpeg either way and would
    # otherwise accumulate in storage forever -- confirm it's actually gone.
    assert raw_key not in fake_storage.objects
    assert raw_key in fake_storage.deleted_keys


async def test_input_longer_than_cap_is_flagged_truncated(
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # tiny.mp4 is 2s; a 1s cap forces truncation. The caller must be told rather
    # than silently losing the second half.
    monkeypatch.setattr(settings, "VIDEO_MAX_DURATION_SECONDS", 1)
    raw_key = "raw/videos/long.mp4"
    fake_storage.objects[raw_key] = fixture_bytes("tiny.mp4")
    upload_id = await _seed_processing(fake_metadata, raw_key)

    result = await compress_video_task(
        raw_storage_key=raw_key, original_filename="long.mp4", upload_id=upload_id
    )

    assert result["status"] == "success"
    assert result["truncated"] is True
    assert result["max_duration_seconds"] == 1
    assert result["duration_seconds"] == pytest.approx(2.0, abs=0.5)

    record = await fake_metadata.get(upload_id, OWNER)
    assert record is not None
    assert record.truncated is True
    assert record.duration_seconds == pytest.approx(2.0, abs=0.5)


async def test_ffmpeg_failure_marks_record_failed_and_deletes_raw(
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
) -> None:
    raw_key = "raw/videos/bad.mp4"
    fake_storage.objects[raw_key] = b"not actually a video, ffmpeg will reject this"
    upload_id = await _seed_processing(fake_metadata, raw_key)

    with pytest.raises(RuntimeError, match="FFmpeg failed"):
        await compress_video_task(
            raw_storage_key=raw_key, original_filename="bad.mp4", upload_id=upload_id
        )

    record = await fake_metadata.get(upload_id, OWNER)
    assert record is not None and record.status == STATUS_FAILED
    assert raw_key not in fake_storage.objects
    assert raw_key in fake_storage.deleted_keys


async def test_ffmpeg_timeout_marks_record_failed_and_deletes_raw(
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_key = "raw/videos/slow.mp4"
    fake_storage.objects[raw_key] = fixture_bytes("tiny.mp4")
    upload_id = await _seed_processing(fake_metadata, raw_key)
    # A real subprocess can't finish inside a 0s window -- forces the
    # asyncio.wait_for timeout path deterministically without a hung process.
    monkeypatch.setattr(settings, "FFMPEG_TIMEOUT_SECONDS", 0)

    with pytest.raises(RuntimeError, match="timed out"):
        await compress_video_task(
            raw_storage_key=raw_key, original_filename="slow.mp4", upload_id=upload_id
        )

    record = await fake_metadata.get(upload_id, OWNER)
    assert record is not None and record.status == STATUS_FAILED
    assert raw_key not in fake_storage.objects
    assert raw_key in fake_storage.deleted_keys


async def test_success_fires_completed_webhook_when_callback_set(
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
    captured_webhooks: list[dict[str, Any]],
) -> None:
    raw_key = "raw/videos/cb.mp4"
    fake_storage.objects[raw_key] = fixture_bytes("tiny.mp4")
    upload_id = await _seed_processing(fake_metadata, raw_key, callback_url="https://h/hook")

    await compress_video_task(
        raw_storage_key=raw_key, original_filename="cb.mp4", upload_id=upload_id
    )

    assert len(captured_webhooks) == 1
    call = captured_webhooks[0]
    assert call["url"] == "https://h/hook"
    assert call["event"] == "video.completed"
    assert call["delivery_id"] == upload_id
    assert call["data"]["status"] == STATUS_READY


async def test_failure_fires_failed_webhook_when_callback_set(
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
    captured_webhooks: list[dict[str, Any]],
) -> None:
    raw_key = "raw/videos/cbfail.mp4"
    fake_storage.objects[raw_key] = b"not a video"
    upload_id = await _seed_processing(fake_metadata, raw_key, callback_url="https://h/hook")

    with pytest.raises(RuntimeError, match="FFmpeg failed"):
        await compress_video_task(
            raw_storage_key=raw_key, original_filename="cbfail.mp4", upload_id=upload_id
        )

    assert len(captured_webhooks) == 1
    assert captured_webhooks[0]["event"] == "video.failed"
    assert captured_webhooks[0]["data"]["status"] == STATUS_FAILED


async def test_no_webhook_when_no_callback_url(
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
    captured_webhooks: list[dict[str, Any]],
) -> None:
    raw_key = "raw/videos/nocb.mp4"
    fake_storage.objects[raw_key] = fixture_bytes("tiny.mp4")
    upload_id = await _seed_processing(fake_metadata, raw_key)  # no callback_url

    await compress_video_task(
        raw_storage_key=raw_key, original_filename="nocb.mp4", upload_id=upload_id
    )

    assert captured_webhooks == []


async def test_upload_deleted_midflight_discards_output_instead_of_orphaning_it(
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
) -> None:
    raw_key = "raw/videos/gone.mp4"
    fake_storage.objects[raw_key] = fixture_bytes("tiny.mp4")
    upload_id = await _seed_processing(fake_metadata, raw_key)
    # The owner DELETEs the upload while it is compressing.
    await fake_metadata.delete(upload_id, OWNER)

    result = await compress_video_task(
        raw_storage_key=raw_key, original_filename="gone.mp4", upload_id=upload_id
    )

    assert result["status"] == "discarded"
    # The compressed object it wrote must not be left orphaned in storage.
    assert not any(key.startswith("videos/") for key in fake_storage.objects)
    assert any(key.startswith("videos/") for key in fake_storage.deleted_keys)
    assert raw_key in fake_storage.deleted_keys
