"""Integration and route tests for POST /upload/file."""

from __future__ import annotations

import time

import httpx
import jwt
import pytest

from app.config import _derive_owner, settings
from app.routers.auth import SCOPE_READ_FILE, SCOPE_UPLOAD_FILE, SCOPE_UPLOAD_IMAGE
from app.services.metadata import KIND_FILE, STATUS_READY
from tests.fakes import InMemoryMetadataStore, InMemoryStorageBackend

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<\n>>\nendobj\ntrailer\n<<\n>>\n%%EOF\n"
MP3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00#\x00\x00" + b"\xff\xfb\x90d\x00\x00\x00\x00"
WAV_BYTES = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00"
ZIP_BYTES = b"PK\x03\x04\x14\x00\x00\x00\x08\x00\x00\x00\x00\x00"
BIN_BYTES = b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
OWNER = _derive_owner("test-token")


async def test_upload_pdf_success(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_storage: InMemoryStorageBackend,
) -> None:
    resp = await client.post(
        "/upload/file",
        headers=auth_headers,
        files={"file": ("report.pdf", PDF_BYTES, "application/pdf")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["kind"] == "file"
    assert body["content_type"] == "application/pdf"
    assert body["original_filename"] == "report.pdf"
    assert body["size_bytes"] == len(PDF_BYTES)
    assert body["visibility"] == "public"
    assert body["url"].endswith(f"/files/{body['id']}/download")

    # Verify DB record
    rec = await fake_metadata.get(body["id"], OWNER)
    assert rec is not None
    assert rec.kind == KIND_FILE
    assert rec.status == STATUS_READY
    assert rec.storage_key.startswith("files/")
    assert rec.storage_key.endswith(".pdf")

    # Verify storage object
    assert fake_storage.objects[rec.storage_key] == PDF_BYTES


async def test_upload_audio_wav_success(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_storage: InMemoryStorageBackend,
) -> None:
    resp = await client.post(
        "/upload/file",
        headers=auth_headers,
        files={"file": ("sound.wav", WAV_BYTES, "audio/wav")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["kind"] == "file"
    assert body["content_type"] == "audio/wav"
    assert body["original_filename"] == "sound.wav"


async def test_upload_zip_success(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_storage: InMemoryStorageBackend,
) -> None:
    resp = await client.post(
        "/upload/file",
        headers=auth_headers,
        files={"file": ("archive.zip", ZIP_BYTES, "application/zip")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["content_type"] == "application/zip"


async def test_upload_file_idempotency_same_visibility(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    # First upload
    resp1 = await client.post(
        "/upload/file",
        headers=auth_headers,
        files={"file": ("doc.pdf", PDF_BYTES, "application/pdf")},
    )
    assert resp1.status_code == 200
    id1 = resp1.json()["id"]

    # Second upload of exact same bytes & visibility
    resp2 = await client.post(
        "/upload/file",
        headers=auth_headers,
        files={"file": ("doc2.pdf", PDF_BYTES, "application/pdf")},
    )
    assert resp2.status_code == 200
    assert resp2.json()["id"] == id1


async def test_upload_file_distinct_visibility_not_deduped(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    # Upload public
    resp1 = await client.post(
        "/upload/file",
        headers=auth_headers,
        files={"file": ("doc.pdf", PDF_BYTES, "application/pdf")},
        data={"visibility": "public"},
    )
    assert resp1.status_code == 200
    id1 = resp1.json()["id"]

    # Upload private with same bytes
    resp2 = await client.post(
        "/upload/file",
        headers=auth_headers,
        files={"file": ("doc.pdf", PDF_BYTES, "application/pdf")},
        data={"visibility": "private"},
    )
    assert resp2.status_code == 200
    id2 = resp2.json()["id"]
    assert id1 != id2


async def test_upload_file_size_limit_exceeded(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "MAX_FILE_UPLOAD_BYTES", 50)
    resp = await client.post(
        "/upload/file",
        headers=auth_headers,
        files={"file": ("big.pdf", b"%PDF-" + b"x" * 100, "application/pdf")},
    )
    assert resp.status_code == 413
    assert "File exceeds 50 bytes" in resp.json()["detail"]


async def test_upload_file_mismatched_content_type_rejected(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    resp = await client.post(
        "/upload/file",
        headers=auth_headers,
        files={"file": ("fake.pdf", b"not a real pdf content", "application/pdf")},
    )
    assert resp.status_code == 400
    assert "File content does not match declared content-type" in resp.json()["detail"]


async def test_upload_file_dangerous_svg_rejected(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    resp = await client.post(
        "/upload/file",
        headers=auth_headers,
        files={
            "file": (
                "danger.svg",
                b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
                "image/svg+xml",
            )
        },
    )
    assert resp.status_code == 400


async def test_upload_file_dangerous_html_rejected(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    resp = await client.post(
        "/upload/file",
        headers=auth_headers,
        files={
            "file": (
                "danger.html",
                b"<!DOCTYPE html><html><body>x</body></html>",
                "text/html",
            )
        },
    )
    assert resp.status_code == 400


async def test_upload_file_auth_scopes(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "unit-test-jwt-secret-that-is-long-enough-for-hs256"
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", secret)

    now = int(time.time())

    # JWT with upload:file scope -> 200
    file_token = jwt.encode(
        {"sub": "tenant-file", "scopes": [SCOPE_UPLOAD_FILE], "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )
    resp = await client.post(
        "/upload/file",
        headers={"Authorization": f"Bearer {file_token}"},
        files={"file": ("doc.pdf", PDF_BYTES, "application/pdf")},
    )
    assert resp.status_code == 200

    # JWT with only upload:image scope -> 403
    img_token = jwt.encode(
        {"sub": "tenant-img", "scopes": [SCOPE_UPLOAD_IMAGE], "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )
    resp = await client.post(
        "/upload/file",
        headers={"Authorization": f"Bearer {img_token}"},
        files={"file": ("doc.pdf", PDF_BYTES, "application/pdf")},
    )
    assert resp.status_code == 403

    # JWT with only read:file scope -> 403
    read_token = jwt.encode(
        {
            "sub": "tenant-read",
            "scopes": [SCOPE_READ_FILE],
            "file": "rec-1",
            "iat": now,
            "exp": now + 300,
        },
        secret,
        algorithm="HS256",
    )
    resp = await client.post(
        "/upload/file",
        headers={"Authorization": f"Bearer {read_token}"},
        files={"file": ("doc.pdf", PDF_BYTES, "application/pdf")},
    )
    assert resp.status_code == 403


async def test_upload_file_with_query_token(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "unit-test-jwt-secret-that-is-long-enough-for-hs256"
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", secret)
    now = int(time.time())
    token = jwt.encode(
        {"sub": "tenant-q", "scopes": [SCOPE_UPLOAD_FILE], "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )
    resp = await client.post(
        f"/upload/file?token={token}",
        files={"file": ("doc.pdf", PDF_BYTES, "application/pdf")},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


async def test_upload_file_parameterized_html_rejected(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    pad = b"A" * 5000 + b"<script>alert(1)</script>"
    resp = await client.post(
        "/upload/file",
        headers=auth_headers,
        files={"file": ("page.html", pad, "text/html; charset=utf-8")},
    )
    assert resp.status_code == 400


async def test_upload_file_unlisted_content_type_rejected(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    resp = await client.post(
        "/upload/file",
        headers=auth_headers,
        files={"file": ("custom.bin", b"\x00\x01\x02\x03", "application/x-anything")},
    )
    assert resp.status_code == 400


async def test_upload_file_junk_bytes_with_declared_image_rejected(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    resp = await client.post(
        "/upload/file",
        headers=auth_headers,
        files={"file": ("fake.png", b"\x00\x01\x02\x03", "image/png")},
    )
    assert resp.status_code == 400
