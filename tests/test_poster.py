"""Video posters: the worker task (real ffmpeg frame-extract -> real pyvips
validate/strip -> WebP, against the tiny.mp4 fixture) and the owner-scoped
on-request route, plus the delete-cascade of a poster with its parent video.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.config import _derive_owner
from app.services.metadata import KIND_IMAGE, KIND_VIDEO, STATUS_PROCESSING, STATUS_READY
from app.tasks import generate_poster_task
from tests.conftest import fixture_bytes
from tests.fakes import InMemoryMetadataStore, InMemoryStorageBackend

OWNER = _derive_owner("test-token")


async def _ready_video(store: InMemoryMetadataStore, storage_key: str) -> str:
    record = await store.create(
        owner=OWNER,
        kind=KIND_VIDEO,
        storage_key=storage_key,
        content_type="video/mp4",
        size_bytes=1,
        status=STATUS_READY,
    )
    return record.id


# --- The poster task (real ffmpeg + pyvips) --------------------------------


async def test_generate_poster_creates_linked_webp_image_record(
    fake_storage: InMemoryStorageBackend, fake_metadata: InMemoryMetadataStore
) -> None:
    video_key = "videos/abc_compressed.mp4"
    fake_storage.objects[video_key] = fixture_bytes("tiny.mp4")
    video_id = await _ready_video(fake_metadata, video_key)

    result = await generate_poster_task(upload_id=video_id, at_seconds=0.0)

    assert result["status"] == "success"
    poster_id = result["poster_id"]

    poster = await fake_metadata.get(poster_id, OWNER)
    assert poster is not None
    assert poster.kind == KIND_IMAGE
    assert poster.content_type == "image/webp"
    assert poster.storage_key.startswith("posters/")
    assert poster.storage_key in fake_storage.objects
    assert poster.width and poster.height  # real dimensions from pyvips

    # The video row now points at its poster.
    video = await fake_metadata.get(video_id, OWNER)
    assert video is not None
    assert video.poster_upload_id == poster_id


async def test_generate_poster_default_timestamp_probes_duration(
    fake_storage: InMemoryStorageBackend, fake_metadata: InMemoryMetadataStore
) -> None:
    # No explicit at_seconds -> the task ffprobes the clip and seeks ~10% in.
    video_key = "videos/def_compressed.mp4"
    fake_storage.objects[video_key] = fixture_bytes("tiny.mp4")
    video_id = await _ready_video(fake_metadata, video_key)

    result = await generate_poster_task(upload_id=video_id)

    assert result["status"] == "success"


async def test_generate_poster_skips_when_video_gone(
    fake_storage: InMemoryStorageBackend, fake_metadata: InMemoryMetadataStore
) -> None:
    result = await generate_poster_task(upload_id="does-not-exist", at_seconds=0.0)
    assert result["status"] == "skipped"


async def test_generate_poster_discards_when_video_deleted_midflight(
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video_key = "videos/gone_compressed.mp4"
    fake_storage.objects[video_key] = fixture_bytes("tiny.mp4")
    video_id = await _ready_video(fake_metadata, video_key)

    # The owner DELETEs the video between load and link: set_poster finds no row.
    async def race_set_poster(vid: str, pid: str) -> None:
        return None

    monkeypatch.setattr(fake_metadata, "set_poster", race_set_poster)

    result = await generate_poster_task(upload_id=video_id, at_seconds=0.0)

    assert result["status"] == "discarded"
    # The poster it wrote must not be left orphaned in storage.
    assert not any(k.startswith("posters/") for k in fake_storage.objects)
    assert any(k.startswith("posters/") for k in fake_storage.deleted_keys)


# --- The poster route ------------------------------------------------------


@pytest.fixture
def poster_enqueue(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class _FakeTask:
        task_id = "poster-task-0"

    async def fake_kiq(**kwargs: Any) -> _FakeTask:
        calls.append(kwargs)
        return _FakeTask()

    monkeypatch.setattr(generate_poster_task, "kiq", fake_kiq)
    return calls


async def test_route_enqueues_poster_for_ready_video(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    poster_enqueue: list[dict[str, Any]],
) -> None:
    video_id = await _ready_video(fake_metadata, "videos/r_compressed.mp4")

    resp = await client.post(f"/files/{video_id}/poster", headers=auth_headers)

    assert resp.status_code == 202
    assert resp.json()["video_id"] == video_id
    assert poster_enqueue[0]["upload_id"] == video_id
    assert poster_enqueue[0]["at_seconds"] is None


async def test_route_passes_at_seconds(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    poster_enqueue: list[dict[str, Any]],
) -> None:
    video_id = await _ready_video(fake_metadata, "videos/t_compressed.mp4")

    resp = await client.post(
        f"/files/{video_id}/poster", headers=auth_headers, data={"at_seconds": "1.5"}
    )

    assert resp.status_code == 202
    assert poster_enqueue[0]["at_seconds"] == 1.5


async def test_route_returns_existing_poster_without_reenqueue(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    poster_enqueue: list[dict[str, Any]],
) -> None:
    poster = await fake_metadata.create(
        owner=OWNER,
        kind=KIND_IMAGE,
        storage_key="posters/p.webp",
        content_type="image/webp",
        size_bytes=10,
        status=STATUS_READY,
        width=320,
        height=240,
    )
    video_id = await _ready_video(fake_metadata, "videos/e_compressed.mp4")
    await fake_metadata.set_poster(video_id, poster.id)

    resp = await client.post(f"/files/{video_id}/poster", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
    assert resp.json()["poster"]["id"] == poster.id
    assert poster_enqueue == []  # not regenerated


async def test_route_409_when_video_not_ready(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    poster_enqueue: list[dict[str, Any]],
) -> None:
    record = await fake_metadata.create(
        owner=OWNER,
        kind=KIND_VIDEO,
        storage_key="raw/videos/p.mp4",
        content_type="video/mp4",
        size_bytes=1,
        status=STATUS_PROCESSING,
    )
    resp = await client.post(f"/files/{record.id}/poster", headers=auth_headers)
    assert resp.status_code == 409
    assert poster_enqueue == []


async def test_route_400_for_non_video(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    poster_enqueue: list[dict[str, Any]],
) -> None:
    record = await fake_metadata.create(
        owner=OWNER,
        kind=KIND_IMAGE,
        storage_key="images/p.webp",
        content_type="image/webp",
        size_bytes=1,
        status=STATUS_READY,
    )
    resp = await client.post(f"/files/{record.id}/poster", headers=auth_headers)
    assert resp.status_code == 400
    assert poster_enqueue == []


async def test_route_404_for_unknown_video(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    poster_enqueue: list[dict[str, Any]],
) -> None:
    resp = await client.post("/files/nope/poster", headers=auth_headers)
    assert resp.status_code == 404


# --- Delete cascade --------------------------------------------------------


async def test_delete_video_cascades_to_poster(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
) -> None:
    fake_storage.objects["posters/p.webp"] = b"webp-bytes"
    poster = await fake_metadata.create(
        owner=OWNER,
        kind=KIND_IMAGE,
        storage_key="posters/p.webp",
        content_type="image/webp",
        size_bytes=10,
        status=STATUS_READY,
    )
    fake_storage.objects["videos/v_compressed.mp4"] = b"mp4-bytes"
    video_id = await _ready_video(fake_metadata, "videos/v_compressed.mp4")
    await fake_metadata.set_poster(video_id, poster.id)

    resp = await client.delete(f"/files/{video_id}", headers=auth_headers)

    assert resp.status_code == 204
    # Both the video and its poster (object + record) are gone.
    assert await fake_metadata.get(video_id, OWNER) is None
    assert await fake_metadata.get(poster.id, OWNER) is None
    assert "videos/v_compressed.mp4" not in fake_storage.objects
    assert "posters/p.webp" not in fake_storage.objects
