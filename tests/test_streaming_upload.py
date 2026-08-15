"""Milestone C: API ingestion streams to disk (bounded memory) and stores from
that temp file, instead of buffering the whole body in RAM. Covers the streaming
reader (incremental hash + size cap + abort cleanup) and each backend's
streaming upload_from_path.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

from app.config import settings
from app.routers.utils import (
    HTTP_499_CLIENT_CLOSED_REQUEST,
    _read_capped,
    _stream_capped_to_temp,
)
from app.services.storage import LocalStorage
from tests.fakes import InMemoryStorageBackend


class _FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


class _DisconnectedRequest:
    async def is_disconnected(self) -> bool:
        return True


def _upload(data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename="v.mp4")


async def test_stream_to_temp_writes_file_and_hashes_incrementally() -> None:
    data = b"a" * (3 * 1024 * 1024 + 123)  # spans several 1MB chunks
    path, size, digest = await _stream_capped_to_temp(
        _upload(data), _FakeRequest(), 50 * 1024 * 1024
    )
    try:
        assert size == len(data)
        # The rolling sha256 must equal a one-shot hash of the same bytes.
        assert digest == hashlib.sha256(data).hexdigest()
        with open(path, "rb") as f:
            assert f.read() == data
    finally:
        os.unlink(path)


async def test_stream_to_temp_413_and_removes_partial_temp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, p = real_mkstemp(*args, **kwargs)
        captured["path"] = p
        return fd, p

    monkeypatch.setattr(tempfile, "mkstemp", spy_mkstemp)

    upload_file = _upload(b"x" * 4096)
    req = _FakeRequest()
    with pytest.raises(HTTPException) as excinfo:
        await _stream_capped_to_temp(upload_file, req, max_bytes=100)
    assert excinfo.value.status_code == 413
    # The partially-written temp file must be cleaned up on abort, not leaked.
    assert not os.path.exists(captured["path"])


async def test_stream_to_temp_aborts_with_499_and_removes_partial_temp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 499 is not a Starlette constant, so this branch previously raised
    # AttributeError (a 500) instead of the intended abort status.
    captured: dict[str, str] = {}
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, p = real_mkstemp(*args, **kwargs)
        captured["path"] = p
        return fd, p

    monkeypatch.setattr(tempfile, "mkstemp", spy_mkstemp)

    upload_file = _upload(b"x" * 4096)
    req = _DisconnectedRequest()
    with pytest.raises(HTTPException) as excinfo:
        await _stream_capped_to_temp(upload_file, req, 50 * 1024 * 1024)

    assert excinfo.value.status_code == HTTP_499_CLIENT_CLOSED_REQUEST
    assert excinfo.value.status_code == 499
    assert not os.path.exists(captured["path"])


async def test_read_capped_aborts_with_499_on_client_disconnect() -> None:
    upload_file = _upload(b"y" * 4096)
    req = _DisconnectedRequest()
    with pytest.raises(HTTPException) as excinfo:
        await _read_capped(upload_file, req, 50 * 1024 * 1024)

    assert excinfo.value.status_code == HTTP_499_CLIENT_CLOSED_REQUEST
    assert excinfo.value.status_code == 499


async def test_local_upload_from_path_streams_to_target(tmp_path: Path) -> None:
    data = b"z" * (2 * 1024 * 1024 + 7)
    src = tmp_path / "src.bin"
    src.write_bytes(data)
    local = LocalStorage(root=str(tmp_path / "store"), public_base_url="")

    obj = await local.upload_from_path(str(src), "raw/videos/x.mp4", "video/mp4")

    assert obj.size == len(data)
    assert obj.key == "raw/videos/x.mp4"
    assert (tmp_path / "store" / "raw/videos/x.mp4").read_bytes() == data


async def test_fake_upload_from_path_default_reads_then_uploads(tmp_path: Path) -> None:
    # The in-memory fake inherits the base default (read file -> upload), so the
    # video route's upload_file_from_path lands the bytes in the fake unchanged.
    src = tmp_path / "s.bin"
    src.write_bytes(b"hello world")
    fake = InMemoryStorageBackend()

    obj = await fake.upload_from_path(str(src), "raw/videos/y.mp4", "video/mp4")

    assert obj.size == len(b"hello world")
    assert fake.objects["raw/videos/y.mp4"] == b"hello world"


async def test_s3_upload_from_path_uses_upload_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.services.storage import S3Storage

    monkeypatch.setattr(settings, "S3_BUCKET", "test-bucket")
    src = tmp_path / "s.bin"
    src.write_bytes(b"data")
    s3 = S3Storage()
    calls: dict[str, object] = {}

    class _FakeS3Client:
        async def upload_file(
            self,
            path: str,
            bucket: str,
            key: str,
            ExtraArgs: dict | None = None,  # noqa: N803
        ) -> None:
            calls.update(path=path, bucket=bucket, key=key, extra=ExtraArgs)

    async def _fake_get_client() -> _FakeS3Client:
        return _FakeS3Client()

    monkeypatch.setattr(s3, "_get_client", _fake_get_client)

    obj = await s3.upload_from_path(str(src), "raw/videos/z.mp4", "video/mp4")

    # upload_file streams from the on-disk path -- the bytes are never handed to
    # boto in memory (unlike upload_fileobj(BytesIO(data))).
    assert calls["path"] == str(src)
    assert calls["key"] == "raw/videos/z.mp4"
    assert calls["extra"] == {"ContentType": "video/mp4"}
    assert obj.size == 4


async def test_gcs_upload_from_path_streams_file_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.services.storage import GCSStorage

    monkeypatch.setattr(settings, "GCS_BUCKET", "test-bucket")
    src = tmp_path / "g.bin"
    src.write_bytes(b"data")
    gcs = GCSStorage()
    calls: dict[str, object] = {}

    class _FakeGCSClient:
        async def upload(
            self,
            bucket: str,
            key: str,
            file_data: object,
            *,
            content_type: str | None = None,
            force_resumable_upload: bool | None = None,
            **kwargs: object,
        ) -> None:
            calls.update(
                bucket=bucket,
                key=key,
                is_file_obj=hasattr(file_data, "read"),
                resumable=force_resumable_upload,
            )

    gcs._client = _FakeGCSClient()  # pre-seed so _get_client() returns it

    obj = await gcs.upload_from_path(str(src), "raw/videos/g.mp4", "video/mp4")

    # A file object is streamed (resumable), not raw bytes loaded into memory.
    assert calls["is_file_obj"] is True
    assert calls["resumable"] is True
    assert obj.size == 4
