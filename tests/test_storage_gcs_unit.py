"""GCS backend URL construction only.

No live GCP credentials/project are available in this environment -- these
are unit tests against a freshly-constructed backend (GCSStorage.__init__
never touches the network; only _get_client() does, which none of these
call), not a live round-trip against a real bucket.
"""

from typing import Any

import aiohttp
import pytest

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
