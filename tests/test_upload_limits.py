from typing import Any

import httpx
import pytest

from app.config import settings
from tests.conftest import fixture_bytes


async def test_image_upload_rejects_oversized_body(
    client: httpx.AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "MAX_IMAGE_UPLOAD_BYTES", 10)
    resp = await client.post(
        "/upload/image",
        headers=auth_headers,
        files={"file": ("big.png", b"x" * 100, "image/png")},
    )
    assert resp.status_code == 413


async def test_image_upload_within_limit_is_not_rejected_for_size(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/upload/image",
        headers=auth_headers,
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    assert resp.status_code != 413


async def test_video_upload_rejects_oversized_body(
    client: httpx.AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "MAX_VIDEO_UPLOAD_BYTES", 10)
    resp = await client.post(
        "/upload/video",
        headers=auth_headers,
        files={"file": ("big.mp4", b"x" * 100, "video/mp4")},
    )
    assert resp.status_code == 413


async def test_video_upload_within_limit_is_not_rejected_for_size(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_enqueue: list[dict[str, Any]],
) -> None:
    resp = await client.post(
        "/upload/video",
        headers=auth_headers,
        files={"file": ("tiny.mp4", fixture_bytes("tiny.mp4"), "video/mp4")},
    )
    assert resp.status_code != 413
    assert len(fake_enqueue) == 1
