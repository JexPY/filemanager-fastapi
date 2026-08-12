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


def _decode_imgproxy_source(imgproxy_url: str) -> str:
    # imgproxy URL shape: {base}/{signature}/{options}/{b64_source}[.{format}]
    b64_source = imgproxy_url.rstrip("/").rsplit("/", 1)[-1]
    if "." in b64_source:
        b64_source = b64_source.rsplit(".", 1)[0]
    padded = b64_source + "=" * (-len(b64_source) % 4)
    return base64.urlsafe_b64decode(padded).decode()


async def test_source_is_local_scheme_for_local_backend(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    # The test env's STORAGE_BACKEND defaults to "local" -- build_source_url
    # swaps in imgproxy's local:// scheme rather than the fake's plain URL. The
    # source is no longer returned directly (raw_url was dropped); it survives
    # only b64-embedded in the signed imgproxy URLs, so assert it there.
    resp = await client.post(
        "/upload/image",
        headers=auth_headers,
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    source = _decode_imgproxy_source(resp.json()["imgproxy_thumbnail_url"])
    assert source.startswith("local:///images/")
    assert "X-Amz-Signature" not in source


async def test_source_is_presigned_when_backend_supports_it(
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
    # A private-bucket-style backend must never sign imgproxy's source over the
    # unsigned direct URL -- the embedded source must be the presigned URL (a
    # private bucket would otherwise be unreachable for imgproxy too).
    source = _decode_imgproxy_source(resp.json()["imgproxy_thumbnail_url"])
    assert "X-Amz-Signature=fake" in source


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
