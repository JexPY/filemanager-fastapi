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
    """Capture the webhook-delivery task *enqueue* instead of hitting Redis/HTTP.
    Delivery no longer runs inline in the compression task -- it's enqueued onto
    its own TaskIQ task (deliver_webhook_task) so a slow receiver can't block the
    worker slot. The compression task only fires the enqueue."""
    calls: list[dict[str, Any]] = []

    class _FakeTask:
        task_id = "webhook-task-0"

    async def fake_kiq(**kwargs: Any) -> _FakeTask:
        calls.append(kwargs)
        return _FakeTask()

    monkeypatch.setattr(tasks_module.deliver_webhook_task, "kiq", fake_kiq)
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

    # Thin task: the result carries only execution state, never domain data.
    # Everything else (key, size, duration, truncated, url) is the
    # Postgres row's job -- asserted below, the single source of truth GET /files
    # reads live. Nothing about the compressed object is sealed in Redis here.
    assert result == {"status": "success", "upload_id": upload_id}

    # The record is flipped to ready and now points at the compressed object.
    record = await fake_metadata.get(upload_id, OWNER)
    assert record is not None
    assert record.status == STATUS_READY
    assert record.storage_key.startswith("videos/")
    assert record.storage_key in fake_storage.objects
    # tiny.mp4 is 2s, well under the 60s default cap -> not truncated, and the
    # probed input duration is persisted on the row.
    assert record.truncated is False
    assert record.duration_seconds == pytest.approx(2.0, abs=0.5)

    # The raw upload is fully consumed by ffmpeg either way and would
    # otherwise accumulate in storage forever -- confirm it's actually gone.
    assert raw_key not in fake_storage.objects
    assert raw_key in fake_storage.deleted_keys


async def test_compression_task_streams_output_via_upload_from_path(
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_key = "raw/videos/stream_test.mp4"
    fake_storage.objects[raw_key] = fixture_bytes("tiny.mp4")
    upload_id = await _seed_processing(fake_metadata, raw_key)

    from_path_called: list[str] = []
    original_upload_from_path = tasks_module.upload_file_from_path

    async def tracking_upload_from_path(
        path: str, key: str, content_type: str = "application/octet-stream"
    ) -> Any:
        from_path_called.append(path)
        return await original_upload_from_path(path, key, content_type)

    async def fail_upload_file(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("upload_file (bytes) should not be called by compress_video_task")

    monkeypatch.setattr(tasks_module, "upload_file_from_path", tracking_upload_from_path)
    monkeypatch.setattr(tasks_module, "upload_file", fail_upload_file)

    result = await compress_video_task(
        raw_storage_key=raw_key, original_filename="stream_test.mp4", upload_id=upload_id
    )
    assert result == {"status": "success", "upload_id": upload_id}
    assert len(from_path_called) == 1


async def test_cropping_within_cap_is_not_truncated(
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
) -> None:
    raw_key = "raw/videos/long.mp4"
    fake_storage.objects[raw_key] = fixture_bytes("tiny.mp4")
    upload_id = await _seed_processing(fake_metadata, raw_key)

    result = await compress_video_task(
        raw_storage_key=raw_key,
        original_filename="long.mp4",
        upload_id=upload_id,
        start_seconds=0.5,
        end_seconds=1.5,
    )

    assert result == {"status": "success", "upload_id": upload_id}

    # 1.5 - 0.5 = 1.0s <= 60s default cap -> not truncated.
    record = await fake_metadata.get(upload_id, OWNER)
    assert record is not None
    assert record.truncated is False
    assert record.duration_seconds == pytest.approx(2.0, abs=0.5)


async def test_input_longer_than_cap_is_flagged_truncated(
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # tiny.mp4 is 2s; a 1s cap forces truncation.
    monkeypatch.setattr(settings, "VIDEO_MAX_DURATION_SECONDS", 1)
    raw_key = "raw/videos/long.mp4"
    fake_storage.objects[raw_key] = fixture_bytes("tiny.mp4")
    upload_id = await _seed_processing(fake_metadata, raw_key)

    result = await compress_video_task(
        raw_storage_key=raw_key,
        original_filename="long.mp4",
        upload_id=upload_id,
    )

    assert result == {"status": "success", "upload_id": upload_id}

    record = await fake_metadata.get(upload_id, OWNER)
    assert record is not None
    assert record.truncated is True
    assert record.duration_seconds == pytest.approx(2.0, abs=0.5)


async def test_cropping_exceeding_cap_is_flagged_truncated(
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1.8 - 0.2 = 1.6s > 1s cap -> truncated.
    monkeypatch.setattr(settings, "VIDEO_MAX_DURATION_SECONDS", 1)
    raw_key = "raw/videos/long_crop.mp4"
    fake_storage.objects[raw_key] = fixture_bytes("tiny.mp4")
    upload_id = await _seed_processing(fake_metadata, raw_key)

    result = await compress_video_task(
        raw_storage_key=raw_key,
        original_filename="long_crop.mp4",
        upload_id=upload_id,
        start_seconds=0.2,
        end_seconds=1.8,
    )

    assert result == {"status": "success", "upload_id": upload_id}

    record = await fake_metadata.get(upload_id, OWNER)
    assert record is not None
    assert record.truncated is True


async def test_end_seconds_past_eof_is_not_flagged_truncated(
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A caller passing a deliberately huge end_seconds to mean "through to the
    # end" gets the whole 2s clip -- nothing is cut, so `truncated` must stay
    # false even though 999 > the 60s cap. Regression: the window used to be
    # taken at face value instead of being clamped to the real input length.
    monkeypatch.setattr(settings, "VIDEO_MAX_DURATION_SECONDS", 60)
    raw_key = "raw/videos/open_ended.mp4"
    fake_storage.objects[raw_key] = fixture_bytes("tiny.mp4")
    upload_id = await _seed_processing(fake_metadata, raw_key)

    result = await compress_video_task(
        raw_storage_key=raw_key,
        original_filename="open_ended.mp4",
        upload_id=upload_id,
        end_seconds=999.0,
    )

    assert result == {"status": "success", "upload_id": upload_id}

    record = await fake_metadata.get(upload_id, OWNER)
    assert record is not None
    assert record.truncated is False


@pytest.mark.parametrize(
    ("input_duration", "start", "end", "expected"),
    [
        # No trim: the whole input.
        (100.0, None, None, 100.0),
        # Start-only: what remains after the seek.
        (100.0, 10.0, None, 90.0),
        # Both bounds, entirely inside the clip: the requested window.
        (100.0, 10.0, 40.0, 30.0),
        # End past EOF: clamped to what actually exists (the fixed bug).
        (2.0, None, 999.0, 2.0),
        (2.0, 0.0, 100.0, 2.0),
        # Start past EOF: nothing left to encode.
        (2.0, 5.0, None, 0.0),
        # Inverted window: no negative durations escape.
        (100.0, 40.0, 10.0, 0.0),
        # Unprobeable input: unknown, never a guess.
        (None, None, None, None),
        (None, 1.0, 5.0, None),
    ],
)
def test_effective_output_duration(
    input_duration: float | None,
    start: float | None,
    end: float | None,
    expected: float | None,
) -> None:
    assert tasks_module._effective_output_duration(input_duration, start, end) == expected


def test_build_ffmpeg_args_defaults() -> None:
    args = tasks_module._build_ffmpeg_args(
        input_source="input.mp4",
        output_path="output.mp4",
        output_format="mp4",
        optimization="balanced",
        start_seconds=None,
        end_seconds=None,
        max_duration_seconds=60,
    )
    assert args[0] == "ffmpeg"
    assert "-y" in args
    assert "-i" in args
    i_idx = args.index("-i")
    assert args[i_idx + 1] == "input.mp4"
    assert "-t" in args
    t_idx = args.index("-t")
    assert args[t_idx + 1] == "60"
    assert "-vf" in args
    vf_idx = args.index("-vf")
    assert args[vf_idx + 1] == "scale='min(1280,iw)':-2"
    assert "-c:v" in args
    cv_idx = args.index("-c:v")
    assert args[cv_idx + 1] == "libx264"
    assert args[-1] == "output.mp4"


def test_build_ffmpeg_args_trim_and_quality_webm_vp9() -> None:
    args = tasks_module._build_ffmpeg_args(
        input_source="input.mp4",
        output_path="output.webm",
        output_format="webm_vp9",
        optimization="quality",
        start_seconds=1.5,
        end_seconds=10.0,
        max_duration_seconds=30,
    )
    assert "-ss" in args
    ss_idx = args.index("-ss")
    assert args[ss_idx + 1] == "1.5"
    assert "-to" in args
    to_idx = args.index("-to")
    assert args[to_idx + 1] == "10.0"
    assert "-t" in args
    t_idx = args.index("-t")
    assert args[t_idx + 1] == "30"
    vf_idx = args.index("-vf")
    assert args[vf_idx + 1] == "scale='min(1920,iw)':-2"
    cv_idx = args.index("-c:v")
    assert args[cv_idx + 1] == "libvpx-vp9"
    ca_idx = args.index("-c:a")
    assert args[ca_idx + 1] == "libopus"
    assert args[-1] == "output.webm"


def test_build_ffmpeg_args_webm_av1() -> None:
    args = tasks_module._build_ffmpeg_args(
        input_source="input.mp4",
        output_path="output.webm",
        output_format="webm_av1",
        optimization="balanced",
        start_seconds=None,
        end_seconds=None,
        max_duration_seconds=None,
    )
    assert "-t" not in args
    cv_idx = args.index("-c:v")
    assert args[cv_idx + 1] == "libsvtav1"
    ca_idx = args.index("-c:a")
    assert args[ca_idx + 1] == "libopus"
    assert args[-1] == "output.webm"


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
    assert call["upload_id"] == upload_id
    assert call["event"] == "video.completed"


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
    assert captured_webhooks[0]["upload_id"] == upload_id
    assert captured_webhooks[0]["event"] == "video.failed"


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
