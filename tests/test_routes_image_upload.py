import httpx

from tests.conftest import fixture_bytes


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
