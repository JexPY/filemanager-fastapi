import httpx
import pyvips

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

    # Verify opaque background (3 bands sRGB, no alpha channel)
    image = pyvips.Image.new_from_buffer(resp.content, "")
    assert not image.hasalpha()
    assert image.bands == 3


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


async def test_qrcode_with_oversized_logo_returns_413(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    oversized_bytes = b"x" * (settings.MAX_QR_LOGO_BYTES + 10)
    resp = await client.post(
        "/generate/qrcode",
        data={"content": "https://example.com"},
        files={"logo": ("huge_logo.png", oversized_bytes, "image/png")},
        headers=auth_headers,
    )
    assert resp.status_code == 413
    assert f"Logo exceeds {settings.MAX_QR_LOGO_BYTES} bytes" in resp.json()["detail"]


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


async def test_qrcode_with_unsupported_svg_logo(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    svg_bytes = b"<svg xmlns='http://www.w3.org/2000/svg'><rect width='10' height='10'/></svg>"
    resp = await client.post(
        "/generate/qrcode",
        data={"content": "https://example.com"},
        files={"logo": ("logo.svg", svg_bytes, "image/svg+xml")},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid logo image"


async def test_qrcode_vcard_valid_and_with_logo(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    # 1. Without logo
    resp = await client.post(
        "/generate/qrcode/vcard",
        data={
            "name": "Doe;John",
            "displayname": "John Doe",
            "phone": "+1234567890",
            "email": "john.doe@example.com",
            "url": "https://example.com",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"

    # 2. With logo
    resp_logo = await client.post(
        "/generate/qrcode/vcard",
        data={"name": "Doe;John", "email": "john.doe@example.com"},
        files={"logo": ("logo.png", fixture_bytes("tiny.png"), "image/png")},
        headers=auth_headers,
    )
    assert resp_logo.status_code == 200
    assert resp_logo.content[:8] == b"\x89PNG\r\n\x1a\n"


async def test_qrcode_vcard_invalid_email(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/generate/qrcode/vcard",
        data={"name": "John Doe", "email": "not-an-email"},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid email address format"


async def test_qrcode_mecard_valid_and_with_logo(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/generate/qrcode/mecard",
        data={"name": "Doe,John", "phone": "+1234567890", "email": "john@example.com"},
        files={"logo": ("logo.png", fixture_bytes("tiny.png"), "image/png")},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


async def test_qrcode_wifi_valid_and_validation(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    # 1. Valid WiFi QR with logo
    resp = await client.post(
        "/generate/qrcode/wifi",
        data={"ssid": "HomeWiFi", "password": "SecretPassword123", "security": "WPA"},
        files={"logo": ("logo.png", fixture_bytes("tiny.png"), "image/png")},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"

    # 2. Invalid security parameter
    resp_bad = await client.post(
        "/generate/qrcode/wifi",
        data={"ssid": "HomeWiFi", "security": "INVALID_SEC"},
        headers=auth_headers,
    )
    assert resp_bad.status_code == 400
    assert "Invalid security type" in resp_bad.json()["detail"]


async def test_qrcode_geo_valid_and_validation(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    # 1. Valid Geo QR with logo
    resp = await client.post(
        "/generate/qrcode/geo",
        data={"lat": 37.7749, "lng": -122.4194},
        files={"logo": ("logo.png", fixture_bytes("tiny.png"), "image/png")},
        headers=auth_headers,
    )
    assert resp.status_code == 200

    # 2. Invalid coordinates out of range
    resp_bad = await client.post(
        "/generate/qrcode/geo",
        data={"lat": 190.0, "lng": -122.4194},
        headers=auth_headers,
    )
    assert resp_bad.status_code == 422


async def test_qrcode_epc_valid_and_validation(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    # 1. Valid EPC QR with logo
    resp = await client.post(
        "/generate/qrcode/epc",
        data={
            "name": "John Doe",
            "iban": "DE89 3704 0044 0532 0130 00",
            "amount": 49.99,
            "text": "Invoice 123",
        },
        files={"logo": ("logo.png", fixture_bytes("tiny.png"), "image/png")},
        headers=auth_headers,
    )
    assert resp.status_code == 200

    # 2. Invalid IBAN format
    resp_bad = await client.post(
        "/generate/qrcode/epc",
        data={"name": "John Doe", "iban": "INVALID_IBAN", "amount": 10.0},
        headers=auth_headers,
    )
    assert resp_bad.status_code == 400
    assert resp_bad.json()["detail"] == "Invalid SEPA IBAN format"
