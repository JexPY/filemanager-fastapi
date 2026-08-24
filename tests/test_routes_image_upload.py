import base64

import httpx
import pytest

import app.services.storage as storage_module
from app.config import _derive_owner, settings
from app.main import app
from tests.conftest import fixture_bytes
from tests.fakes import InMemoryMetadataStore, InMemoryStorageBackend


async def test_valid_image_upload_succeeds(
    client: httpx.AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Default upload with public base URL: returns direct public object URL and no thumbnail_url
    monkeypatch.setattr(settings, "LOCAL_PUBLIC_BASE_URL", "http://localhost:9000")
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
    assert "url" in body
    assert body["url"].endswith(".webp")
    assert "thumbnail_url" not in body
    assert "imgproxy_thumbnail_url" not in body
    assert "medium_url" not in body

    # Without public base URL: falls back to canonical /files/{id}/download
    monkeypatch.setattr(settings, "LOCAL_PUBLIC_BASE_URL", "")
    resp_fallback = await client.post(
        "/upload/image",
        headers=auth_headers,
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    assert resp_fallback.status_code == 200
    body_fb = resp_fallback.json()
    assert body_fb["url"].endswith(f"/files/{body_fb['id']}/download")

    # Upload with ?thumbnail=true returns thumbnail_url with .webp extension
    resp_thumb = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    assert resp_thumb.status_code == 200
    body_thumb = resp_thumb.json()
    assert "thumbnail_url" in body_thumb
    assert body_thumb["thumbnail_url"].endswith(".webp")


def _decode_imgproxy_source(imgproxy_url: str) -> str:
    # imgproxy URL shape: {base}/{signature}/{options}/{b64_source}[.{format}]
    b64_source = imgproxy_url.rstrip("/").rsplit("/", 1)[-1]
    if "." in b64_source:
        b64_source = b64_source.rsplit(".", 1)[0]
    padded = b64_source + "=" * (-len(b64_source) % 4)
    return base64.urlsafe_b64decode(padded).decode()


async def test_source_is_local_scheme_for_local_backend(
    client: httpx.AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The test env's STORAGE_BACKEND defaults to "local" -- build_source_url
    # swaps in imgproxy's local:// scheme rather than the fake's plain URL. The
    # source is no longer returned directly (raw_url was dropped); it survives
    # only b64-embedded in the signed imgproxy URLs, so assert it there.
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(settings, "LOCAL_PUBLIC_BASE_URL", "")
    resp = await client.post(
        "/upload/image?thumbnail=true",
        headers=auth_headers,
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    source = _decode_imgproxy_source(resp.json()["thumbnail_url"])
    assert source.startswith("local:///images/")
    assert "X-Amz-Signature" not in source


async def test_imgproxy_source_is_never_presigned_and_matches_the_record(
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    """An imgproxy signature never expires, so the source it wraps must not either.

    This asserts the inverse of the behaviour it replaces. The upload response
    used to embed a *presigned* source (1h TTL by default) while
    `UploadRecord.to_public` embedded the plain object URL -- so the same image
    had two different imgproxy URLs, i.e. two CDN cache keys, and the pair handed
    back at upload time silently rotted after an hour while looking permanent.

    Both now derive from `signed_image_url`, so the two must be byte-identical.
    """
    # STORAGE_BACKEND must actually be non-local here too: build_source_url's
    # local:// override is keyed on this setting, not on the fake's
    # presign_capable flag, since a real deployment can't run local storage
    # with a presigning backend at the same time.
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(storage_module, "_storage", InMemoryStorageBackend(presign_capable=True))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/upload/image?thumbnail=true",
            headers=auth_headers,
            files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
        )
    assert resp.status_code == 200
    body = resp.json()

    source = _decode_imgproxy_source(body["thumbnail_url"])
    assert "X-Amz-Signature" not in source, "a presigned source would expire under a permanent URL"
    assert source == "http://fake-storage/images/" + source.rsplit("/", 1)[-1]

    # Same image, same imgproxy URL, whichever endpoint you ask.
    record = await fake_metadata.get(body["id"], _derive_owner("test-token"))
    assert record is not None
    record_source = _decode_imgproxy_source(record.to_public()["thumbnail_url"])
    assert record_source == source


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
