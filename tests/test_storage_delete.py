"""Delete-path idempotency across backends (Milestone A).

DELETE /files/{id} removes the storage object first and only drops the Postgres
row on success, so a transient failure leaves a retryable record. That retry must
be safe: deleting an already-gone object has to succeed, not 502. LocalStorage
(unlink(missing_ok=True)) and S3 (delete_object returns 204 on a missing key) are
idempotent by construction; GCS was not -- gcloud-aio-storage raises
aiohttp.ClientResponseError(status=404) for a missing object, which the backend
used to re-raise as a StorageError. These tests pin the fixed behavior.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import aiohttp
import pytest

from app.config import settings
from app.services.storage import GCSStorage, LocalStorage, StorageError
from tests.fakes import InMemoryStorageBackend


def _response_error(status: int) -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(Mock(), (), status=status)


class _FakeGCSClient:
    """Stands in for gcloud.aio.storage.Storage: records deletes and can raise a
    scripted exception on the Nth call to simulate a missing object / server error.
    """

    def __init__(self, raises: Exception | None = None, raises_after: int = 0) -> None:
        self._raises = raises
        self._raises_after = raises_after
        self.calls = 0

    async def delete(self, bucket: str, object_name: str, *args: object, **kwargs: object) -> str:
        self.calls += 1
        if self._raises is not None and self.calls > self._raises_after:
            raise self._raises
        return ""


def _gcs_with_client(monkeypatch: pytest.MonkeyPatch, client: _FakeGCSClient) -> GCSStorage:
    monkeypatch.setattr(settings, "GCS_BUCKET", "test-bucket")
    gcs = GCSStorage()
    gcs._client = client  # pre-seed so _get_client() never builds a real Storage
    return gcs


async def test_gcs_delete_swallows_404(monkeypatch: pytest.MonkeyPatch) -> None:
    # A missing object (404) is not an error -- the delete is idempotent.
    client = _FakeGCSClient(raises=_response_error(404))
    gcs = _gcs_with_client(monkeypatch, client)
    await gcs.delete("videos/gone.mp4")  # must not raise
    assert client.calls == 1


async def test_gcs_delete_retry_after_success_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The real race: first delete succeeds, the row-delete fails, the client
    # retries -> the object is already gone (404) and the retry must succeed.
    client = _FakeGCSClient(raises=_response_error(404), raises_after=1)
    gcs = _gcs_with_client(monkeypatch, client)
    await gcs.delete("videos/x.mp4")  # first: real delete
    await gcs.delete("videos/x.mp4")  # retry: 404, swallowed
    assert client.calls == 2


async def test_gcs_delete_reraises_non_404(monkeypatch: pytest.MonkeyPatch) -> None:
    # A genuine server-side failure (500) must still surface as StorageError.
    gcs = _gcs_with_client(monkeypatch, _FakeGCSClient(raises=_response_error(500)))
    with pytest.raises(StorageError):
        await gcs.delete("videos/x.mp4")


async def test_gcs_delete_reraises_generic_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A transport failure (no HTTP status at all) must still surface as StorageError.
    gcs = _gcs_with_client(monkeypatch, _FakeGCSClient(raises=aiohttp.ClientError()))
    with pytest.raises(StorageError):
        await gcs.delete("videos/x.mp4")


async def test_local_delete_missing_key_is_noop(tmp_path: Path) -> None:
    local = LocalStorage(root=str(tmp_path), public_base_url="")
    await local.delete("videos/never-existed.mp4")  # must not raise
    await local.delete("videos/never-existed.mp4")  # and again


async def test_in_memory_fake_delete_is_idempotent() -> None:
    fake = InMemoryStorageBackend()
    await fake.upload(b"data", "videos/x.mp4", "video/mp4")
    await fake.delete("videos/x.mp4")
    await fake.delete("videos/x.mp4")  # already gone -- still fine
    assert "videos/x.mp4" not in fake.objects
