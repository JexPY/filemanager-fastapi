import httpx

from app.config import settings

from .conftest import fixture_bytes


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


async def test_content_exceeding_qr_capacity_is_rejected_without_leaking_detail(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    # Under the character cap, but 4-byte-per-char emoji content still blows
    # past QR's byte-mode capacity -- exercises segno's DataOverflowError
    # path distinct from the Form max_length rejection above (422).
    content = "\U0001f600" * 1500
    resp = await client.post("/generate/qrcode", data={"content": content}, headers=auth_headers)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail == "Invalid QR content"
    assert "DataOverflow" not in detail
    assert "segno" not in detail.lower()


async def test_qrcode_with_custom_colors(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/generate/qrcode",
        data={"content": "https://example.com", "dark": "#1a1a2e", "light": "#eaeaea"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"


async def test_qrcode_with_logo(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    resp = await client.post(
        "/generate/qrcode",
        data={"content": "https://example.com"},
        files={"logo": ("logo.png", fixture_bytes("tiny.png"), "image/png")},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"


async def test_qrcode_invalid_dark_color(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/generate/qrcode",
        data={"content": "https://example.com", "dark": "notacolor"},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid color format, use #rrggbb"


async def test_qrcode_invalid_scale(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    for bad_scale in (0, 99):
        resp = await client.post(
            "/generate/qrcode",
            data={"content": "https://example.com", "scale": bad_scale},
            headers=auth_headers,
        )
        assert resp.status_code == 422


async def test_qrcode_with_invalid_logo_bytes(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/generate/qrcode",
        data={"content": "https://example.com"},
        files={"logo": ("logo.png", b"notanimage", "image/png")},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid logo image"
