from typing import Any

import httpx
import pytest

from app.routers.utils import _sanitize_extension
from tests.conftest import fixture_bytes


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("video.mp4", "mp4"),
        ("video.MP4", "mp4"),
        ("no_extension", "bin"),
        ("../../etc/passwd.mp4", "mp4"),  # the path lives in `filename`, not just the extension
        ("trailing.dot.", "bin"),
        ("weird.m/p4", "mp4"),  # '/' is stripped, not treated as a separator
        ("weird.m\x00p4", "mp4"),
        ("long." + "a" * 40, "aaaaaaaa"),
        ("unicode.\u202e", "bin"),  # right-to-left override char -> stripped to nothing
        ("", "bin"),
    ],
)
def test_sanitize_extension(filename: str, expected: str) -> None:
    assert _sanitize_extension(filename) == expected


async def test_video_upload_key_is_never_traversal(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_enqueue: list[dict[str, Any]],
) -> None:
    resp = await client.post(
        "/upload/video",
        headers=auth_headers,
        files={"file": ("../../../etc/passwd.mp4", fixture_bytes("tiny.mp4"), "video/mp4")},
    )
    assert resp.status_code == 202
    assert len(fake_enqueue) == 1
    raw_key = fake_enqueue[0]["raw_storage_key"]
    assert raw_key.startswith("raw/videos/")
    # exactly one more "/" -- the fixed prefix -- nothing from the filename leaked in
    assert raw_key.count("/") == 2
    assert raw_key.endswith(".mp4")
