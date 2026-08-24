"""Every upload lands a metadata record, owned by the calling token."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.config import _derive_owner, settings
from tests.conftest import fixture_bytes
from tests.fakes import InMemoryMetadataStore

# The auth_headers fixture configures this single bare token.
TEST_OWNER = _derive_owner("test-token")


async def test_image_upload_creates_a_ready_record(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    resp = await client.post(
        "/upload/image",
        headers=auth_headers,
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    assert resp.status_code == 200
    body = resp.json()

    records = await fake_metadata.list(TEST_OWNER)
    assert len(records) == 1
    record = records[0]
    assert record.id == body["id"]
    assert record.owner == TEST_OWNER
    assert record.kind == "image"
    assert record.status == "ready"
    assert record.storage_key.startswith("images/")
    assert record.content_type == "image/webp"
    assert record.dominant_color is not None
    assert record.blur_data_url is not None
    assert body["dominant_color"] == record.dominant_color
    assert body["blur_data_url"] == record.blur_data_url


async def test_video_upload_creates_a_processing_record_with_task_id(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_enqueue: list[dict[str, Any]],
) -> None:
    resp = await client.post(
        "/upload/video",
        headers=auth_headers,
        files={"file": ("tiny.mp4", fixture_bytes("tiny.mp4"), "video/mp4")},
    )
    assert resp.status_code == 202
    body = resp.json()

    record = await fake_metadata.get(body["id"], TEST_OWNER)
    assert record is not None
    assert record.kind == "video"
    assert record.status == "processing"
    assert record.storage_key.startswith("raw/videos/")
    assert record.original_filename == "tiny.mp4"
    # set_task_id ran after enqueue, linking the record to its compression task.
    assert record.task_id == body["task_id"]

    # The worker was handed the record id so it can mark it ready later.
    assert fake_enqueue[0]["upload_id"] == body["id"]


async def test_uploads_are_scoped_to_the_calling_token(
    client: httpx.AsyncClient,
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "FILE_MANAGER_BEARER_TOKENS", "alice:tok-a,bob:tok-b")
    png = fixture_bytes("tiny.png")

    await client.post(
        "/upload/image",
        headers={"Authorization": "Bearer tok-a"},
        files={"file": ("a.png", png, "image/png")},
    )
    await client.post(
        "/upload/image",
        headers={"Authorization": "Bearer tok-b"},
        files={"file": ("b.png", png, "image/png")},
    )

    # Each token sees exactly its own upload -- the per-token audit trail.
    assert len(await fake_metadata.list("alice")) == 1
    assert len(await fake_metadata.list("bob")) == 1


async def test_upload_image_custom_imgproxy_url(
    client: httpx.AsyncClient,
) -> None:
    png = fixture_bytes("tiny.png")
    resp = await client.post(
        "/upload/image",
        headers={"Authorization": "Bearer test-token"},
        files={"file": ("tiny.png", png, "image/png")},
        data={
            "width": 1024,
            "height": 768,
            "fit": "fill",
            "format": "webp",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "custom_url" in body
    assert "/rs:fill:1024:768/" in body["custom_url"]
    assert body["custom_url"].endswith(".webp")


async def test_upload_image_no_custom_url_for_a_no_op_format_request(
    client: httpx.AsyncClient,
) -> None:
    """format="webp" alone (no width/height/fit) is not a real customization --
    every processed image is already stored as webp, so this avoids a pointless
    extra URL."""
    png = fixture_bytes("tiny.png")
    resp = await client.post(
        "/upload/image",
        headers={"Authorization": "Bearer test-token"},
        files={"file": ("tiny.png", png, "image/png")},
        data={"fit": "auto", "format": "webp"},
    )
    assert resp.status_code == 200
    assert "custom_url" not in resp.json()


async def test_to_public_url_is_the_canonical_route_for_every_kind(
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a public base URL, `url` is the app's own download route.

    It is the one URL that is permanent (a backend switch, a CDN change, or a
    visibility flip do not touch it) and the one that resolves for a private
    record. imgproxy URLs are a separate, additive, public-only accelerator --
    they are never the canonical address, because an imgproxy URL is an
    unexpiring bearer capability with no ownership check.
    """
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(settings, "LOCAL_PUBLIC_BASE_URL", "")
    image = await fake_metadata.create(
        owner=TEST_OWNER,
        kind="image",
        storage_key="images/test_to_public.webp",
        content_type="image/webp",
        size_bytes=123,
        status="ready",
        visibility="public",
    )
    public_data = image.to_public()
    assert public_data["storage_key"] == "images/test_to_public.webp"
    assert "renditions" in public_data
    assert "content_hash" not in public_data
    assert public_data["url"].endswith(f"/files/{image.id}/download")
    assert "/rs:auto/" not in public_data["url"]
    assert "/rs:fill:300:300:0/g:no/" in public_data["thumbnail_url"]

    video = await fake_metadata.create(
        owner=TEST_OWNER,
        kind="video",
        storage_key="videos/test_to_public.mp4",
        content_type="video/mp4",
        size_bytes=456,
        status="ready",
        visibility="public",
    )
    # A real poster record -- poster_storage_key is resolved fresh from the
    # linked row (mirroring Postgres's LEFT JOIN), not persisted at link time,
    # so set_poster needs an id that actually exists to produce a poster_url.
    poster = await fake_metadata.create(
        owner=TEST_OWNER,
        kind="image",
        storage_key="posters/test_to_public.webp",
        content_type="image/webp",
        size_bytes=10,
        status="ready",
        visibility="public",
    )
    video_with_poster = await fake_metadata.set_poster(video.id, poster.id)
    assert video_with_poster is not None
    video_data = video_with_poster.to_public()
    # Identical canonical shape to the image above -- that is the point.
    assert video_data["url"].endswith(f"/files/{video.id}/download")
    assert "/rs:auto/" in video_data["poster_url"]
    assert "/files/" not in video_data["poster_url"]
    assert video_data["poster_upload_id"] == poster.id


async def test_to_public_url_uses_direct_object_url_when_public_base_configured(
    fake_metadata: InMemoryMetadataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(settings, "LOCAL_PUBLIC_BASE_URL", "http://localhost:9000")
    image = await fake_metadata.create(
        owner=TEST_OWNER,
        kind="image",
        storage_key="images/test_direct.webp",
        content_type="image/webp",
        size_bytes=123,
        status="ready",
        visibility="public",
        renditions={"thumbnail": "images/test_direct_t300.webp"},
    )
    public_data = image.to_public()
    assert public_data["url"] == "http://localhost:9000/images/test_direct.webp"
    assert public_data["thumbnail_url"] == "http://localhost:9000/images/test_direct_t300.webp"


async def test_to_public_withholds_accelerator_urls_from_private_records(
    fake_metadata: InMemoryMetadataStore,
) -> None:
    """A private record gets the canonical download route and nothing else.

    `thumbnail_url` bypasses the app entirely, so emitting it for
    a private record would hand out a permanent, unauthenticated way to read it.
    """
    private = await fake_metadata.create(
        owner=TEST_OWNER,
        kind="image",
        storage_key="images/secret.webp",
        content_type="image/webp",
        size_bytes=123,
        status="ready",
        visibility="private",
    )
    data = private.to_public()

    assert data["url"].endswith(f"/files/{private.id}/download")
    assert "thumbnail_url" not in data
    assert "storage_key" not in data
    assert "renditions" not in data
