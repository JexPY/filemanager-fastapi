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
    assert body["original_filename"] == "tiny.png"

    # Verify in DB
    record = await fake_metadata.get_by_id(body["id"])
    assert record is not None
    assert record.visibility == "public"
    assert record.original_filename == "tiny.png"


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
    assert body["succeeded"] == 2
    assert body["failed"] == 0
    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert body["items"][0]["status"] == "success"
    assert body["items"][0]["original_filename"] == "tiny1.png"
    assert body["items"][1]["status"] == "success"
    assert body["items"][1]["original_filename"] == "tiny2.png"

    for item in body["items"]:
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


async def test_bulk_image_upload_per_file_limit_exceeded_isolates_error(
    client: httpx.AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "MAX_IMAGE_UPLOAD_BYTES", 500)
    files = [
        ("files", ("small.png", fixture_bytes("tiny.png"), "image/png")),
        ("files", ("big.png", b"x" * 600, "image/png")),
    ]
    resp = await client.post("/upload/images", headers=auth_headers, files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["succeeded"] == 1
    assert body["failed"] == 1
    assert body["total"] == 2
    assert len(body["items"]) == 2

    assert body["items"][0]["status"] == "success"
    assert body["items"][0]["original_filename"] == "small.png"

    assert body["items"][1]["status"] == "error"
    assert body["items"][1]["code"] == "too_large"
    assert body["items"][1]["original_filename"] == "big.png"
    assert "id" not in body["items"][1]


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

    # Second upload of the same bytes alongside an invalid file
    resp2 = await client.post(
        "/upload/images",
        headers=auth_headers,
        files=[
            ("files", ("tiny2.png", file_bytes, "image/png")),
            ("files", ("tiny3.svg", fixture_bytes("tiny.svg"), "image/svg+xml")),
        ],
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["succeeded"] == 1
    assert body2["failed"] == 1
    assert body2["total"] == 2
    assert len(body2["items"]) == 2

    # First item: deduplicated success echoing current request's original_filename
    assert body2["items"][0]["status"] == "success"
    assert body2["items"][0]["id"] == id1
    assert body2["items"][0]["original_filename"] == "tiny2.png"

    # Second item: validation error
    assert body2["items"][1]["status"] == "error"
    assert body2["items"][1]["code"] == "invalid_image"
    assert body2["items"][1]["original_filename"] == "tiny3.svg"
    assert "id" not in body2["items"][1]


async def test_bulk_image_upload_positional_identity_and_ordering(
    client: httpx.AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure 1:1 positional index alignment is strictly preserved across all
    success and failure types."""
    monkeypatch.setattr(settings, "MAX_IMAGE_UPLOAD_BYTES", 500)
    files = [
        ("files", ("f0_valid.png", fixture_bytes("tiny.png"), "image/png")),
        ("files", ("f1_corrupt.png", fixture_bytes("corrupt.bin"), "image/png")),
        ("files", ("f2_oversized.png", b"x" * 600, "image/png")),
        ("files", ("f3_valid.png", fixture_bytes("tiny.png"), "image/png")),
    ]
    resp = await client.post("/upload/images", headers=auth_headers, files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["succeeded"] == 2
    assert body["failed"] == 2
    assert body["total"] == 4
    assert len(body["items"]) == 4

    # Slot 0: valid
    assert body["items"][0]["status"] == "success"
    assert body["items"][0]["original_filename"] == "f0_valid.png"
    assert "id" in body["items"][0]

    # Slot 1: corrupt bytes
    assert body["items"][1]["status"] == "error"
    assert body["items"][1]["code"] == "invalid_image"
    assert body["items"][1]["original_filename"] == "f1_corrupt.png"
    assert "id" not in body["items"][1]

    # Slot 2: oversized
    assert body["items"][2]["status"] == "error"
    assert body["items"][2]["code"] == "too_large"
    assert body["items"][2]["original_filename"] == "f2_oversized.png"
    assert "id" not in body["items"][2]

    # Slot 3: valid
    assert body["items"][3]["status"] == "success"
    assert body["items"][3]["original_filename"] == "f3_valid.png"
    assert "id" in body["items"][3]


async def test_bulk_image_upload_aggregate_limit_exceeded(
    client: httpx.AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure aggregate memory budget is respected and slots past budget return
    batch_too_large while preserving 1:1 positional indexing."""
    raw_img = fixture_bytes("tiny.png")  # 284 bytes
    monkeypatch.setattr(settings, "MAX_IMAGE_UPLOAD_BYTES", 500)
    # Allow 2 files (2 * 284 = 568 bytes), 3rd file onwards exceeds 500 bytes aggregate budget
    monkeypatch.setattr(settings, "MAX_BULK_UPLOAD_TOTAL_BYTES", 500)

    files = [
        ("files", ("f0_ok.png", raw_img, "image/png")),
        ("files", ("f1_ok.png", raw_img, "image/png")),
        ("files", ("f2_over_budget.png", raw_img, "image/png")),
        ("files", ("f3_over_budget.png", raw_img, "image/png")),
    ]
    resp = await client.post("/upload/images", headers=auth_headers, files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["succeeded"] == 2
    assert body["failed"] == 2
    assert body["total"] == 4
    assert len(body["items"]) == 4

    assert body["items"][0]["status"] == "success"
    assert body["items"][0]["original_filename"] == "f0_ok.png"
    assert "id" in body["items"][0]

    assert body["items"][1]["status"] == "success"
    assert body["items"][1]["original_filename"] == "f1_ok.png"
    assert "id" in body["items"][1]

    assert body["items"][2]["status"] == "error"
    assert body["items"][2]["code"] == "batch_too_large"
    assert body["items"][2]["original_filename"] == "f2_over_budget.png"
    assert "id" not in body["items"][2]

    assert body["items"][3]["status"] == "error"
    assert body["items"][3]["code"] == "batch_too_large"
    assert body["items"][3]["original_filename"] == "f3_over_budget.png"
    assert "id" not in body["items"][3]


async def test_bulk_image_upload_10_files_aggregate_budget(
    client: httpx.AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_img = fixture_bytes("tiny.png")  # 284 bytes
    monkeypatch.setattr(settings, "MAX_IMAGE_UPLOAD_BYTES", 500)
    # Budget fits 3 files (3 * 284 = 852 > 800)
    monkeypatch.setattr(settings, "MAX_BULK_UPLOAD_TOTAL_BYTES", 800)

    files = [("files", (f"file_{i}.png", raw_img, "image/png")) for i in range(10)]
    resp = await client.post("/upload/images", headers=auth_headers, files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 10
    assert len(body["items"]) == 10
    assert body["succeeded"] == 3
    assert body["failed"] == 7

    for i in range(3):
        assert body["items"][i]["status"] == "success"
        assert body["items"][i]["original_filename"] == f"file_{i}.png"

    for i in range(3, 10):
        assert body["items"][i]["status"] == "error"
        assert body["items"][i]["code"] == "batch_too_large"
        assert body["items"][i]["original_filename"] == f"file_{i}.png"


async def test_bulk_image_upload_single_oversized_vs_batch_too_large(
    client: httpx.AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_img = fixture_bytes("tiny.png")  # 284 bytes
    monkeypatch.setattr(settings, "MAX_IMAGE_UPLOAD_BYTES", 500)
    monkeypatch.setattr(settings, "MAX_BULK_UPLOAD_TOTAL_BYTES", 500)

    files = [
        ("files", ("f0_ok.png", raw_img, "image/png")),
        ("files", ("f1_single_oversized.png", b"x" * 600, "image/png")),
        ("files", ("f2_ok.png", raw_img, "image/png")),
        ("files", ("f3_batch_overflow.png", raw_img, "image/png")),
    ]
    resp = await client.post("/upload/images", headers=auth_headers, files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 4
    assert len(body["items"]) == 4
    assert body["succeeded"] == 2
    assert body["failed"] == 2

    # Slot 0: ok (total_bytes becomes 284)
    assert body["items"][0]["status"] == "success"
    # Slot 1: single file oversized -> too_large (total_bytes stays 284)
    assert body["items"][1]["status"] == "error"
    assert body["items"][1]["code"] == "too_large"
    # Slot 2: ok (total_bytes was 284 < 500, becomes 568)
    assert body["items"][2]["status"] == "success"
    # Slot 3: batch overflow -> batch_too_large (total_bytes was 568 >= 500)
    assert body["items"][3]["status"] == "error"
    assert body["items"][3]["code"] == "batch_too_large"


async def test_bulk_image_upload_filename_sanitization(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    long_name = "a" * 300 + ".png"
    files = [
        ("files", ("../../evil/secret.png", fixture_bytes("tiny.png"), "image/png")),
        ("files", (long_name, fixture_bytes("tiny.png"), "image/png")),
    ]
    resp = await client.post("/upload/images", headers=auth_headers, files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["succeeded"] == 2
    assert body["items"][0]["original_filename"] == "secret.png"
    assert len(body["items"][1]["original_filename"]) <= 255
