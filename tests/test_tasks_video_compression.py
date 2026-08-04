import pytest

from app.config import settings
from app.tasks import compress_video_task
from tests.conftest import fixture_bytes
from tests.fakes import InMemoryStorageBackend


async def test_successful_compression_uploads_output_and_deletes_raw(
    fake_storage: InMemoryStorageBackend,
) -> None:
    raw_key = "raw/videos/test123.mp4"
    fake_storage.objects[raw_key] = fixture_bytes("tiny.mp4")

    result = await compress_video_task(raw_storage_key=raw_key, original_filename="tiny.mp4")

    assert result["status"] == "success"
    assert result["key"].startswith("videos/")
    assert result["key"] in fake_storage.objects
    # The raw upload is fully consumed by ffmpeg either way and would
    # otherwise accumulate in storage forever -- confirm it's actually gone.
    assert raw_key not in fake_storage.objects
    assert raw_key in fake_storage.deleted_keys


async def test_ffmpeg_failure_still_deletes_raw(fake_storage: InMemoryStorageBackend) -> None:
    raw_key = "raw/videos/bad.mp4"
    fake_storage.objects[raw_key] = b"not actually a video, ffmpeg will reject this"

    with pytest.raises(RuntimeError, match="FFmpeg failed"):
        await compress_video_task(raw_storage_key=raw_key, original_filename="bad.mp4")

    assert raw_key not in fake_storage.objects
    assert raw_key in fake_storage.deleted_keys


async def test_ffmpeg_timeout_still_deletes_raw(
    fake_storage: InMemoryStorageBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_key = "raw/videos/slow.mp4"
    fake_storage.objects[raw_key] = fixture_bytes("tiny.mp4")
    # A real subprocess can't finish inside a 0s window -- forces the
    # asyncio.wait_for timeout path deterministically without a hung process.
    monkeypatch.setattr(settings, "FFMPEG_TIMEOUT_SECONDS", 0)

    with pytest.raises(RuntimeError, match="timed out"):
        await compress_video_task(raw_storage_key=raw_key, original_filename="slow.mp4")

    assert raw_key not in fake_storage.objects
    assert raw_key in fake_storage.deleted_keys
