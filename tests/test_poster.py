"""Video posters: the worker task (real ffmpeg frame-extract -> real pyvips
validate/strip -> WebP, against the tiny.mp4 fixture) and the owner-scoped
on-request route, plus the delete-cascade of a poster with its parent video.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import app.services.storage as storage_module
from app.config import _derive_owner, settings
from app.services.metadata import (
    KIND_IMAGE,
    KIND_VIDEO,
    STATUS_PROCESSING,
    STATUS_READY,
    MetadataError,
)
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
    assert poster.storage_key.startswith("private/posters/")
    assert poster.storage_key in fake_storage.objects
    assert poster.width
    assert poster.height  # real dimensions from pyvips
    # No _t300/_m960 rendition objects: generate_poster_task must pass
    # generate_renditions=False through to validate_and_strip_image, since a
    # poster never gets its own materialized rendition (see CLAUDE.md).
    assert not poster.renditions
    assert not any("posters/" in k and "_t300" in k for k in fake_storage.objects)
    assert not any("posters/" in k and "_m960" in k for k in fake_storage.objects)

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
    assert not any("posters/" in k for k in fake_storage.objects)
    assert any("posters/" in k for k in fake_storage.deleted_keys)


async def test_generate_poster_discards_when_set_poster_raises(
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A *raised* MetadataError from set_poster (a transient store failure,
    not the video-deleted race) must get the same cleanup as the None case
    above -- both the poster object and the poster row it just created
    already exist by the time set_poster is called."""
    video_key = "videos/flaky_compressed.mp4"
    fake_storage.objects[video_key] = fixture_bytes("tiny.mp4")
    video_id = await _ready_video(fake_metadata, video_key)

    async def raising_set_poster(vid: str, pid: str) -> None:
        raise MetadataError("transient store failure")

    monkeypatch.setattr(fake_metadata, "set_poster", raising_set_poster)

    with pytest.raises(MetadataError):
        await generate_poster_task(upload_id=video_id, at_seconds=0.0)

    # Neither the object nor the row it just created may be left behind.
    assert not any("posters/" in k for k in fake_storage.objects)
    assert any("posters/" in k for k in fake_storage.deleted_keys)
    assert not any(r.kind == KIND_IMAGE for r in fake_metadata.records.values())


async def test_generate_poster_discards_when_create_raises(
    fake_storage: InMemoryStorageBackend,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A *raised* MetadataError from store.create (writing the poster's own
    row) must not leave the just-uploaded poster object orphaned -- no row
    was ever created here, so only the object needs cleanup."""
    video_key = "videos/flakycreate_compressed.mp4"
    fake_storage.objects[video_key] = fixture_bytes("tiny.mp4")
    video_id = await _ready_video(fake_metadata, video_key)

    real_create = fake_metadata.create

    async def raising_create(*args: Any, **kwargs: Any):
        if kwargs.get("kind") == KIND_IMAGE:
            raise MetadataError("transient store failure")
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(fake_metadata, "create", raising_create)

    with pytest.raises(MetadataError):
        await generate_poster_task(upload_id=video_id, at_seconds=0.0)

    assert not any("posters/" in k for k in fake_storage.objects)
    assert any("posters/" in k for k in fake_storage.deleted_keys)


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
        f"/files/{video_id}/poster", headers=auth_headers, data={"poster_seconds": "1.5"}
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


# --- poster_url (unified) -------------------------------------------------


async def test_video_record_carries_direct_poster_url_when_public_base_url_set(
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a public base URL is set, poster_url resolves directly to the CDN object URL."""
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(settings, "S3_PUBLIC_BASE_URL", "https://cdn.example.com")
    monkeypatch.setattr(
        storage_module, "_storage", InMemoryStorageBackend(base_url="https://cdn.example.com")
    )

    poster = await fake_metadata.create(
        owner=OWNER,
        kind=KIND_IMAGE,
        storage_key="posters/p1.webp",
        content_type="image/webp",
        size_bytes=10,
        status=STATUS_READY,
        visibility="public",
    )
    video_id = await _ready_video(fake_metadata, "videos/withposter_compressed.mp4")
    await fake_metadata.set_visibility(video_id, OWNER, "public")
    await fake_metadata.set_poster(video_id, poster.id)

    video = await fake_metadata.get(video_id, OWNER)
    assert video is not None
    public_view = video.to_public()

    assert public_view["poster_url"] == "https://cdn.example.com/posters/p1.webp"


async def test_poster_url_falls_back_to_imgproxy_without_public_base_url(
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a configured public base URL (e.g. local backend), poster_url
    falls back to signed imgproxy."""
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(settings, "LOCAL_PUBLIC_BASE_URL", "")
    poster = await fake_metadata.create(
        owner=OWNER,
        kind=KIND_IMAGE,
        storage_key="posters/p2.webp",
        content_type="image/webp",
        size_bytes=10,
        status=STATUS_READY,
        visibility="public",
    )
    video_id = await _ready_video(fake_metadata, "videos/noposterurl_compressed.mp4")
    await fake_metadata.set_visibility(video_id, OWNER, "public")
    await fake_metadata.set_poster(video_id, poster.id)

    video = await fake_metadata.get(video_id, OWNER)
    assert video is not None
    public_view = video.to_public()

    assert "/rs:auto/" in public_view["poster_url"]
    assert public_view["poster_url"].endswith(".webp")


async def test_poster_url_omitted_not_broken_when_poster_row_is_gone(
    fake_metadata: InMemoryMetadataStore,
) -> None:
    """poster_upload_id can point at a row that no longer resolves (the
    poster was deleted independently, or -- in Postgres -- the LEFT JOIN
    simply doesn't find it). The fallback here used to be
    `self.poster_upload_id` itself: a bare record id, not a storage key,
    which built a dead/404 poster_url instead of just omitting it.
    poster_url must be absent, not wrong."""
    video_id = await _ready_video(fake_metadata, "videos/dangling_compressed.mp4")
    await fake_metadata.set_visibility(video_id, OWNER, "public")
    # Link to a poster id that was never created.
    await fake_metadata.set_poster(video_id, "does-not-exist")

    video = await fake_metadata.get(video_id, OWNER)
    assert video is not None
    public_view = video.to_public()

    assert public_view["poster_upload_id"] == "does-not-exist"
    assert "poster_url" not in public_view
