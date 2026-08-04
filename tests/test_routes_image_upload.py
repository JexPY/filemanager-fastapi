import base64

import httpx
import pytest

import app.services.storage as storage_module
from app.main import app
from tests.conftest import fixture_bytes
from tests.fakes import InMemoryStorageBackend


async def test_valid_image_upload_succeeds(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/upload/image",
        headers=auth_headers,
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["dimensions"] == {"width": 8, "height": 8}
    assert "imgproxy_thumbnail_url" in body
    assert "imgproxy_optimized_url" in body


async def test_raw_url_is_plain_object_url_when_backend_does_not_presign(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/upload/image",
        headers=auth_headers,
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    assert "X-Amz-Signature" not in resp.json()["raw_url"]


async def test_raw_url_is_presigned_when_backend_supports_it(
    monkeypatch: pytest.MonkeyPatch, auth_headers: dict[str, str]
) -> None:
    monkeypatch.setattr(storage_module, "_storage", InMemoryStorageBackend(presign_capable=True))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/upload/image",
            headers=auth_headers,
            files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
        )
    assert resp.status_code == 200
    body = resp.json()
    # A private-bucket-style backend must never return the unsigned direct
    # URL, and imgproxy's source must be built from the same presigned URL
    # (a private bucket would otherwise be unreachable for imgproxy too).
    assert "X-Amz-Signature=fake" in body["raw_url"]

    # imgproxy_thumbnail_url shape: {base}/{signature}/{options}/{b64_source}
    b64_source = body["imgproxy_thumbnail_url"].rstrip("/").rsplit("/", 1)[-1]
    padded = b64_source + "=" * (-len(b64_source) % 4)
    decoded_source = base64.urlsafe_b64decode(padded).decode()
    assert decoded_source == body["raw_url"]


async def test_svg_upload_is_rejected(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/upload/image",
        headers=auth_headers,
        files={"file": ("tiny.svg", fixture_bytes("tiny.svg"), "image/svg+xml")},
    )
    assert resp.status_code == 400


async def test_corrupt_bytes_upload_is_rejected(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/upload/image",
        headers=auth_headers,
        files={"file": ("fake.png", fixture_bytes("corrupt.bin"), "image/png")},
    )
    assert resp.status_code == 400
