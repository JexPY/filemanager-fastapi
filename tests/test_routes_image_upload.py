import base64

import httpx
import pytest

import app.services.storage as storage_module
from app.config import settings
from app.main import app
from tests.conftest import fixture_bytes
from tests.fakes import InMemoryMetadataStore, InMemoryStorageBackend


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
    assert body["id"]  # a metadata record id the client can list/get/delete by
    assert body["dimensions"] == {"width": 8, "height": 8}
    assert "imgproxy_thumbnail_url" in body
    assert "imgproxy_optimized_url" in body


async def test_raw_url_is_local_source_scheme_for_local_backend(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    # The test env's STORAGE_BACKEND defaults to "local" -- build_source_url
    # swaps in imgproxy's local:// scheme rather than the fake's plain URL.
    resp = await client.post(
        "/upload/image",
        headers=auth_headers,
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    body = resp.json()
    assert body["raw_url"].startswith("local:///images/")
    assert "X-Amz-Signature" not in body["raw_url"]


async def test_raw_url_is_presigned_when_backend_supports_it(
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    # STORAGE_BACKEND must actually be non-local here too: build_source_url's
    # local:// override is keyed on this setting, not on the fake's
    # presign_capable flag, since a real deployment can't run local storage
    # with a presigning backend at the same time.
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
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
