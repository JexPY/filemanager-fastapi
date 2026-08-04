import httpx

from app.config import settings


async def test_valid_qrcode_returns_png(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/generate/qrcode", data={"content": "https://example.com"}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"


async def test_oversized_content_is_rejected(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    too_long = "x" * (settings.MAX_QR_CONTENT_LENGTH + 1)
    resp = await client.post("/generate/qrcode", data={"content": too_long}, headers=auth_headers)
    assert resp.status_code == 422
