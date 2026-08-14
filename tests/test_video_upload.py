"""Tests for POST /upload/video parameters: visibility and poster_seconds."""

from typing import Any

import httpx

from tests.conftest import fixture_bytes
from tests.fakes import InMemoryMetadataStore


async def test_video_upload_visibility_public_and_private(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    # 1. Default visibility should be public
    resp_default = await client.post(
        "/upload/video",
        headers=auth_headers,
        files={"file": ("clip1.mp4", fixture_bytes("tiny.mp4"), "video/mp4")},
    )
    assert resp_default.status_code == 202
    rec_default = await fake_metadata.get_by_id(resp_default.json()["id"])
    assert rec_default is not None
    assert rec_default.visibility == "public"

    # 2. Explicit visibility=private (use different content to avoid deduplication)
    resp_private = await client.post(
        "/upload/video",
        headers=auth_headers,
        data={"visibility": "private"},
        files={"file": ("clip2.mp4", fixture_bytes("tiny.mp4") + b"extra", "video/mp4")},
    )
    assert resp_private.status_code == 202
    rec_private = await fake_metadata.get_by_id(resp_private.json()["id"])
    assert rec_private is not None
    assert rec_private.visibility == "private"


async def test_video_upload_poster_seconds_passed_to_task(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_enqueue: list[dict[str, Any]],
) -> None:
    resp = await client.post(
        "/upload/video",
        headers=auth_headers,
        data={"poster_seconds": "1.5"},
        files={"file": ("clip.mp4", fixture_bytes("tiny.mp4"), "video/mp4")},
    )
    assert resp.status_code == 202
    assert len(fake_enqueue) == 1
    assert fake_enqueue[0]["poster_seconds"] == 1.5


async def test_video_upload_rejects_unsupported_content_type(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    resp = await client.post(
        "/upload/video",
        headers=auth_headers,
        files={"file": ("fake.txt", b"plain text content", "text/plain")},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Unsupported video format"
