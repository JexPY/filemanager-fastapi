"""DELETE /files/{id} -- owner-scoped deletion of the object and its record."""

from __future__ import annotations

import httpx
import pytest

from app.config import _derive_owner
from app.services.metadata import UploadRecord
from app.services.storage import StorageError
from tests.fakes import InMemoryMetadataStore, InMemoryStorageBackend

OWNER = _derive_owner("test-token")


async def _seed(
    storage: InMemoryStorageBackend, metadata: InMemoryMetadataStore, owner: str, key: str
) -> UploadRecord:
    storage.objects[key] = b"bytes"
    return await metadata.create(
        owner=owner,
        kind="image",
        storage_key=key,
        content_type="image/webp",
        size_bytes=5,
        status="ready",
    )


async def test_delete_removes_object_and_record(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
) -> None:
    record = await _seed(fake_storage, fake_metadata, OWNER, "images/a.webp")

    resp = await client.delete(f"/files/{record.id}", headers=auth_headers)
    assert resp.status_code == 204

    assert "images/a.webp" not in fake_storage.objects
    assert "images/a.webp" in fake_storage.deleted_keys
    assert await fake_metadata.get(record.id, OWNER) is None
    # And it's gone from the listing.
    assert (await client.get(f"/files/{record.id}", headers=auth_headers)).status_code == 404


async def test_delete_is_owner_scoped_and_leaves_object_intact(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
) -> None:
    other = await _seed(fake_storage, fake_metadata, "someone-else", "images/x.webp")

    resp = await client.delete(f"/files/{other.id}", headers=auth_headers)
    assert resp.status_code == 404  # 404 (not 403): another owner's id is invisible
    # Neither the object nor the record was touched.
    assert "images/x.webp" in fake_storage.objects
    assert "images/x.webp" not in fake_storage.deleted_keys
    assert await fake_metadata.get(other.id, "someone-else") is not None


async def test_delete_unknown_id_404(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    assert (await client.delete("/files/nope", headers=auth_headers)).status_code == 404


async def test_delete_requires_auth(
    client: httpx.AsyncClient,
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
) -> None:
    record = await _seed(fake_storage, fake_metadata, OWNER, "images/a.webp")
    assert (await client.delete(f"/files/{record.id}")).status_code == 401


async def test_storage_failure_keeps_the_record(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = await _seed(fake_storage, fake_metadata, OWNER, "images/a.webp")

    async def boom(key: str) -> None:
        raise StorageError("backend down")

    monkeypatch.setattr(fake_storage, "delete", boom)

    resp = await client.delete(f"/files/{record.id}", headers=auth_headers)
    assert resp.status_code == 502
    # The object couldn't be deleted, so the record must remain (retryable),
    # never stranding an object with no record pointing at it.
    assert await fake_metadata.get(record.id, OWNER) is not None
