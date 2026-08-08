"""Idempotent video upload: identical raw bytes (per owner) attach to the
existing job instead of staging + compressing the same input twice.

Keyed on the raw *input* hash (the compressed output is nondeterministic). A
`processing` match returns 202 (attach to the in-flight compression); a `ready`
match returns 200 (already available); a `failed` match does NOT dedupe, so a
bad input can be retried.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.config import _derive_owner, settings
from tests.conftest import fixture_bytes
from tests.fakes import InMemoryMetadataStore, InMemoryStorageBackend

OWNER = _derive_owner("test-token")


def _raw_videos(storage: InMemoryStorageBackend) -> list[str]:
    return [k for k in storage.objects if k.startswith("raw/videos/")]


async def _post_video(
    client: httpx.AsyncClient, headers: dict[str, str], name: str = "clip.mp4"
) -> httpx.Response:
    return await client.post(
        "/upload/video",
        headers=headers,
        files={"file": (name, fixture_bytes("tiny.mp4"), "video/mp4")},
    )


async def test_reupload_while_processing_attaches_to_the_inflight_job(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
    fake_enqueue: list[dict[str, Any]],
) -> None:
    first = await _post_video(client, auth_headers)
    second = await _post_video(client, auth_headers)

    assert first.status_code == 202
    assert first.json()["status"] == "accepted"

    # The first upload is still `processing`; the duplicate attaches to it (202,
    # same record + task), without staging a second object or enqueuing again.
    assert second.status_code == 202
    body = second.json()
    assert body["status"] == "duplicate"
    assert body["record_status"] == "processing"
    assert body["id"] == first.json()["id"]
    assert body["task_id"] == first.json()["task_id"]

    assert len(_raw_videos(fake_storage)) == 1
    assert len(await fake_metadata.list(OWNER)) == 1
    assert len(fake_enqueue) == 1


async def test_reupload_after_ready_returns_200_duplicate(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_enqueue: list[dict[str, Any]],
) -> None:
    first = await _post_video(client, auth_headers)
    upload_id = first.json()["id"]
    # Simulate the worker finishing compression.
    await fake_metadata.mark_ready(upload_id, storage_key="videos/x_compressed.mp4", size_bytes=1)

    second = await _post_video(client, auth_headers)

    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "duplicate"
    assert body["record_status"] == "ready"
    assert body["id"] == upload_id
    # No second compression was enqueued.
    assert len(fake_enqueue) == 1


async def test_failed_video_is_not_deduplicated_and_reprocesses(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_enqueue: list[dict[str, Any]],
) -> None:
    first = await _post_video(client, auth_headers)
    await fake_metadata.mark_failed(first.json()["id"])

    second = await _post_video(client, auth_headers)

    # A failed prior attempt must not short-circuit -- the retry is a fresh job.
    assert second.status_code == 202
    assert second.json()["status"] == "accepted"
    assert second.json()["id"] != first.json()["id"]
    assert len(fake_enqueue) == 2


async def test_video_dedup_is_scoped_to_the_owner(
    client: httpx.AsyncClient,
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
    fake_enqueue: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "FILE_MANAGER_BEARER_TOKENS", "alice:tok-a,bob:tok-b")

    await _post_video(client, {"Authorization": "Bearer tok-a"})
    await _post_video(client, {"Authorization": "Bearer tok-b"})

    # Same bytes, different owners -> two independent jobs, no cross-tenant dedup.
    assert len(_raw_videos(fake_storage)) == 2
    assert len(await fake_metadata.list("alice")) == 1
    assert len(await fake_metadata.list("bob")) == 1
    assert len(fake_enqueue) == 2
