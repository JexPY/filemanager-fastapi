"""Idempotent image upload: identical bytes dedupe to one object + record."""

from __future__ import annotations

import httpx
import pytest

from app.config import _derive_owner, settings
from tests.conftest import fixture_bytes
from tests.fakes import InMemoryMetadataStore, InMemoryStorageBackend

OWNER = _derive_owner("test-token")


def _stored_images(storage: InMemoryStorageBackend) -> list[str]:
    return [k for k in storage.objects if k.startswith("images/")]


async def test_reupload_of_identical_bytes_is_deduplicated(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
) -> None:
    png = fixture_bytes("tiny.png")
    first = await client.post(
        "/upload/image", headers=auth_headers, files={"file": ("a.png", png, "image/png")}
    )
    second = await client.post(
        "/upload/image", headers=auth_headers, files={"file": ("b.png", png, "image/png")}
    )

    assert first.status_code == second.status_code == 200
    # Same record id returned; exactly one image (main + thumbnail) exists.
    assert first.json()["id"] == second.json()["id"]
    assert len(_stored_images(fake_storage)) == 3  # main image + thumbnail + medium renditions
    assert len(await fake_metadata.list(OWNER)) == 1
    # The deduplicated response is fully shaped, dimensions included.
    assert second.json()["dimensions"] == {"width": 8, "height": 8}


async def test_distinct_images_are_not_deduplicated(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
) -> None:
    png = await client.post(
        "/upload/image",
        headers=auth_headers,
        files={"file": ("a.png", fixture_bytes("tiny.png"), "image/png")},
    )
    jpg = await client.post(
        "/upload/image",
        headers=auth_headers,
        files={"file": ("b.jpg", fixture_bytes("tiny.jpg"), "image/jpeg")},
    )

    assert png.json()["id"] != jpg.json()["id"]
    assert len(_stored_images(fake_storage)) == 6  # 2 main + 2 thumbnails + 2 mediums
    assert len(await fake_metadata.list(OWNER)) == 2


async def test_dedup_is_scoped_to_the_owner(
    client: httpx.AsyncClient,
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "FILE_MANAGER_BEARER_TOKENS", "alice:tok-a,bob:tok-b")
    png = fixture_bytes("tiny.png")

    await client.post(
        "/upload/image",
        headers={"Authorization": "Bearer tok-a"},
        files={"file": ("x.png", png, "image/png")},
    )
    await client.post(
        "/upload/image",
        headers={"Authorization": "Bearer tok-b"},
        files={"file": ("x.png", png, "image/png")},
    )

    # Same bytes, different owners -> two separate objects and records, no
    # cross-tenant dedup.
    assert len(_stored_images(fake_storage)) == 6  # 2 main + 2 thumbnails + 2 mediums
    assert len(await fake_metadata.list("alice")) == 1
    assert len(await fake_metadata.list("bob")) == 1
