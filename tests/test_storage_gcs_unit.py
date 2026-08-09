"""GCS backend URL construction only.

No live GCP credentials/project are available in this environment -- these
are unit tests against a freshly-constructed backend (GCSStorage.__init__
never touches the network; only _get_client() does, which none of these
call), not a live round-trip against a real bucket.
"""

from typing import Any

import aiohttp
import pytest

import app.services.storage as storage_module
from app.config import settings
from app.services.storage import GCSStorage, StorageError


def test_object_url_uses_public_base_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GCS_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "GCS_PUBLIC_BASE_URL", "https://cdn.example.com")
    backend = GCSStorage()
    assert backend._object_url("images/x.webp") == "https://cdn.example.com/images/x.webp"


def test_object_url_falls_back_to_storage_googleapis_com(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GCS_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "GCS_PUBLIC_BASE_URL", "")
    backend = GCSStorage()
    assert (
        backend._object_url("images/x.webp")
        == "https://storage.googleapis.com/test-bucket/images/x.webp"
    )


def test_object_url_is_independent_of_local_public_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression check for the bug this commit fixes: GCS and Local used to
    # share a single PUBLIC_BASE_URL setting.
    monkeypatch.setattr(settings, "GCS_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "GCS_PUBLIC_BASE_URL", "")
    monkeypatch.setattr(settings, "LOCAL_PUBLIC_BASE_URL", "https://local-cdn.example.com")
    backend = GCSStorage()
    assert "local-cdn" not in backend._object_url("images/x.webp")


class _FakeGCSClient:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def upload(self, *args: Any, **kwargs: Any) -> None:
        raise self._exc

    async def download(self, *args: Any, **kwargs: Any) -> bytes:
        raise self._exc

    async def delete(self, *args: Any, **kwargs: Any) -> None:
        raise self._exc


async def test_aiohttp_client_error_is_wrapped_as_storage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "GCS_BUCKET", "test-bucket")
    backend = GCSStorage()
    backend._client = _FakeGCSClient(aiohttp.ClientConnectionError("connection reset"))

    with pytest.raises(StorageError):
        await backend.upload(b"x", "key", "text/plain")


async def test_unrelated_exception_is_not_swallowed_as_storage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression check: the old bare `except Exception` would have silently
    # misreported a real bug (e.g. an AttributeError from a code change) as
    # a generic "storage unavailable" StorageError.
    monkeypatch.setattr(settings, "GCS_BUCKET", "test-bucket")
    backend = GCSStorage()
    backend._client = _FakeGCSClient(AttributeError("a real bug, not a storage failure"))

    with pytest.raises(AttributeError):
        await backend.upload(b"x", "key", "text/plain")


# --- V4 signed URLs (no live GCP: sign locally is mocked) ------------------
#
# The real signing (gcloud-aio-storage's Blob.get_signed_url) needs a service
# account private key, which isn't present here. These verify the plumbing:
# that presigned_get_url constructs a blob for the right key, asks it to sign
# for the requested (clamped) expiration, and returns/​wraps the result.


class _FakeSignBlob:
    def __init__(self, name: str, url: str = "https://signed.example/x?sig=abc") -> None:
        self.name = name
        self._url = url
        self.signed_for: int | None = None

    async def get_signed_url(self, expiration: int, **kwargs: Any) -> str:
        self.signed_for = expiration
        return self._url


class _FakeSignBucket:
    last_blob: _FakeSignBlob | None = None

    def __init__(self, client: Any, bucket: str) -> None:
        self.bucket = bucket

    def new_blob(self, key: str) -> _FakeSignBlob:
        blob = _FakeSignBlob(key)
        _FakeSignBucket.last_blob = blob
        return blob


async def test_presigned_get_url_signs_locally_and_returns_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "GCS_BUCKET", "test-bucket")
    monkeypatch.setattr(storage_module, "Bucket", _FakeSignBucket)
    backend = GCSStorage()
    backend._client = object()  # bypass _get_client()'s network build

    url = await backend.presigned_get_url("videos/v_compressed.mp4", expires_in=3600)
    assert url == "https://signed.example/x?sig=abc"
    assert _FakeSignBucket.last_blob is not None
    assert _FakeSignBucket.last_blob.name == "videos/v_compressed.mp4"
    assert _FakeSignBucket.last_blob.signed_for == 3600


async def test_presigned_get_url_clamps_expiration_to_7_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "GCS_BUCKET", "test-bucket")
    monkeypatch.setattr(storage_module, "Bucket", _FakeSignBucket)
    backend = GCSStorage()
    backend._client = object()

    await backend.presigned_get_url("videos/v.mp4", expires_in=10_000_000)
    assert _FakeSignBucket.last_blob is not None
    assert _FakeSignBucket.last_blob.signed_for == 604800  # GCS V4 hard cap


async def test_presigned_get_url_wraps_signing_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BoomBucket:
        def __init__(self, client: Any, bucket: str) -> None:
            pass

        def new_blob(self, key: str) -> Any:
            class _BoomBlob:
                async def get_signed_url(self, expiration: int, **kwargs: Any) -> str:
                    raise ValueError("private key is invalid or unsupported")

            return _BoomBlob()

    monkeypatch.setattr(settings, "GCS_BUCKET", "test-bucket")
    monkeypatch.setattr(storage_module, "Bucket", _BoomBucket)
    backend = GCSStorage()
    backend._client = object()

    with pytest.raises(StorageError):
        await backend.presigned_get_url("videos/v.mp4")
