import httpx
import pytest

from app.config import settings
from tests.conftest import fixture_bytes
from tests.fakes import InMemoryMetadataStore


async def test_upload_image_creates_public_record(
    client: httpx.AsyncClient, auth_headers: dict[str, str], fake_metadata: InMemoryMetadataStore
) -> None:
    resp = await client.post(
        "/upload/image",
        headers=auth_headers,
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"

    # Verify in DB
    record = await fake_metadata.get_by_id(body["id"])
    assert record is not None
    assert record.visibility == "public"


async def test_bulk_image_upload_success(
    client: httpx.AsyncClient, auth_headers: dict[str, str], fake_metadata: InMemoryMetadataStore
) -> None:
    resp = await client.post(
        "/upload/images",
        headers=auth_headers,
        files=[
            ("files", ("tiny1.png", fixture_bytes("tiny.png"), "image/png")),
            ("files", ("tiny2.png", fixture_bytes("tiny.png"), "image/png")),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert len(body["items"]) == 2
    for item in body["items"]:
        assert item["status"] == "success"
        record = await fake_metadata.get_by_id(item["id"])
        assert record is not None
        assert record.visibility == "public"


async def test_bulk_image_upload_empty_batch(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    # Testing with no files
    resp = await client.post(
        "/upload/images",
        headers=auth_headers,
        # missing files completely should be caught by FastAPI
    )
    assert resp.status_code == 422


async def test_bulk_image_upload_per_file_limit_exceeded(
    client: httpx.AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "MAX_IMAGE_UPLOAD_BYTES", 50)
    files = [
        ("files", ("small.png", b"x" * 20, "image/png")),
        ("files", ("big.png", b"x" * 60, "image/png")),
    ]
    resp = await client.post("/upload/images", headers=auth_headers, files=files)
    assert resp.status_code == 413
    assert resp.json()["detail"] == "File exceeds 50 bytes"


async def test_bulk_image_upload_too_many_files(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    files = [("files", (f"file{i}.png", b"x", "image/png")) for i in range(11)]
    resp = await client.post("/upload/images", headers=auth_headers, files=files)
    assert resp.status_code == 400
    assert "Maximum of 10 files allowed" in resp.json()["detail"]


async def test_bulk_image_upload_idempotency(
    client: httpx.AsyncClient, auth_headers: dict[str, str], fake_metadata: InMemoryMetadataStore
) -> None:
    file_bytes = fixture_bytes("tiny.png")

    # First upload
    resp1 = await client.post(
        "/upload/images",
        headers=auth_headers,
        files=[("files", ("tiny1.png", file_bytes, "image/png"))],
    )
    assert resp1.status_code == 200
    id1 = resp1.json()["items"][0]["id"]

    # Second upload of the same bytes
    resp2 = await client.post(
        "/upload/images",
        headers=auth_headers,
        files=[
            ("files", ("tiny2.png", file_bytes, "image/png")),
            # Will fail validation, should be skipped
            ("files", ("tiny3.png", fixture_bytes("tiny.svg"), "image/svg+xml")),
        ],
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["count"] == 1  # Second item fails validation
    assert body2["items"][0]["id"] == id1  # Deduplicated
