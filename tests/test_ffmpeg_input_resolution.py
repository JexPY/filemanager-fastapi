"""_resolve_ffmpeg_input: hand ffmpeg an in-place source, never download the
object into worker RAM first (Milestone B). local -> an on-disk path; s3/gcp ->
a presigned URL; neither available -> StorageError.
"""

from __future__ import annotations

import os

import pytest

from app.config import settings
from app.services.storage import StorageError
from app.tasks import _resolve_ffmpeg_input
from tests.fakes import InMemoryStorageBackend


async def test_local_backend_returns_readable_on_disk_path(
    fake_storage: InMemoryStorageBackend,
) -> None:
    fake_storage.objects["raw/videos/x.mp4"] = b"the-bytes"
    source = await _resolve_ffmpeg_input("raw/videos/x.mp4")
    # A real, readable path (not a URL) holding exactly the stored bytes: ffmpeg
    # reads it directly, nothing is buffered in RAM.
    assert os.path.isfile(source)
    with open(source, "rb") as f:
        assert f.read() == b"the-bytes"


async def test_object_store_backend_returns_presigned_url(
    fake_storage: InMemoryStorageBackend,
) -> None:
    # An object-store backend has no shared filesystem, so ffmpeg gets a
    # presigned URL to read over HTTPS instead of a local path.
    fake_storage._presign_capable = True
    fake_storage.objects["raw/videos/y.mp4"] = b"data"
    source = await _resolve_ffmpeg_input("raw/videos/y.mp4")
    assert source.startswith("http")
    assert "X-Amz-Signature=fake" in source


async def test_presigned_url_uses_the_ffmpeg_input_ttl(
    fake_storage: InMemoryStorageBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_storage._presign_capable = True
    fake_storage.objects["raw/videos/z.mp4"] = b"data"
    monkeypatch.setattr(settings, "FFMPEG_INPUT_URL_TTL_SECONDS", 4242)
    source = await _resolve_ffmpeg_input("raw/videos/z.mp4")
    assert "expires=4242" in source  # the fake encodes expires_in into the URL


async def test_backend_with_neither_path_nor_presign_raises(
    fake_storage: InMemoryStorageBackend,
) -> None:
    # A non-presign backend that also can't produce a local path (the object is
    # missing) can't hand ffmpeg an input -> surfaced as StorageError, never a
    # silent empty read.
    with pytest.raises(StorageError):
        await _resolve_ffmpeg_input("raw/videos/missing.mp4")
