"""Cross-origin access control.

CORS is what makes the direct-to-service upload pattern work at all: a consuming
site's JavaScript POSTs a file straight here and has to read the response to
learn the record id. The failure mode without it is quiet rather than loud -- a
multipart POST carrying its credential as `?token=` is a CORS-*simple* request,
so the upload succeeds and the object is stored, but the browser withholds the
response and the caller never learns the id. These tests pin the headers that
prevent that.

`app.main.app` is built once at import, before any fixture can monkeypatch
settings, so each test mounts `configure_cors` onto a throwaway app instead.
That exercises the real wiring, not a re-implementation of it.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.config import settings
from app.main import configure_cors

ALLOWED = "https://fixcar.ge"
FOREIGN = "https://evil.example"


def _app_with_cors() -> FastAPI:
    """A minimal app carrying the same CORS wiring the real one uses.

    Reads the configured origins from `settings`, exactly as the real app does,
    so the `cors_env` fixture is what drives it."""
    app = FastAPI()

    @app.get("/probe")
    def probe() -> dict[str, bool]:
        return {"ok": True}

    configure_cors(app)
    return app


@pytest.fixture
def cors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CORS_ALLOWED_ORIGINS", f"{ALLOWED},https://www.fixcar.ge")


async def _request(app: FastAPI, method: str, url: str, **kwargs: object) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, url, **kwargs)  # type: ignore[arg-type]


# --- configuration parsing ---------------------------------------------------


def test_origins_parse_from_a_comma_separated_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CORS_ALLOWED_ORIGINS", " https://a.example , https://b.example ")
    assert settings.parsed_cors_origins == ["https://a.example", "https://b.example"]


def test_trailing_slash_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A browser sends `Origin` with no trailing slash, and Starlette compares
    verbatim -- so a configured `https://x.example/` would never match."""
    monkeypatch.setattr(settings, "CORS_ALLOWED_ORIGINS", "https://x.example/")
    assert settings.parsed_cors_origins == ["https://x.example"]


def test_blank_config_parses_to_no_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CORS_ALLOWED_ORIGINS", "")
    assert settings.parsed_cors_origins == []


def test_middleware_is_not_mounted_without_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backend-to-backend deployments must keep byte-identical responses."""
    monkeypatch.setattr(settings, "CORS_ALLOWED_ORIGINS", "")
    assert configure_cors(FastAPI()) is False


def test_middleware_is_mounted_with_origins(cors_env: None) -> None:
    assert configure_cors(FastAPI()) is True


# --- preflight ---------------------------------------------------------------


async def test_preflight_from_an_allowed_origin_is_granted(cors_env: None) -> None:
    resp = await _request(
        _app_with_cors(),
        "OPTIONS",
        "/probe",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED
    assert "POST" in resp.headers["access-control-allow-methods"]


async def test_preflight_from_a_foreign_origin_gets_no_grant(cors_env: None) -> None:
    resp = await _request(
        _app_with_cors(),
        "OPTIONS",
        "/probe",
        headers={"Origin": FOREIGN, "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in resp.headers


async def test_preflight_advertises_the_authorization_header(cors_env: None) -> None:
    """Header-capable clients send `Authorization`; without this in the allow
    list their preflight fails and only the `?token=` fallback would work."""
    resp = await _request(
        _app_with_cors(),
        "OPTIONS",
        "/probe",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert "authorization" in resp.headers["access-control-allow-headers"].lower()


# --- actual requests ---------------------------------------------------------


async def test_response_to_an_allowed_origin_is_readable(cors_env: None) -> None:
    """The whole point: the caller must be able to read the body (the record id)."""
    resp = await _request(_app_with_cors(), "GET", "/probe", headers={"Origin": ALLOWED})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED


async def test_response_to_a_foreign_origin_is_not_readable(cors_env: None) -> None:
    resp = await _request(_app_with_cors(), "GET", "/probe", headers={"Origin": FOREIGN})
    assert "access-control-allow-origin" not in resp.headers


async def test_credentials_are_not_allowed(cors_env: None) -> None:
    """Auth here is bearer/`?token=`, never a cookie. Advertising credential
    support would invite cookie auth and the CSRF surface that comes with it."""
    resp = await _request(_app_with_cors(), "GET", "/probe", headers={"Origin": ALLOWED})
    assert "access-control-allow-credentials" not in resp.headers


async def test_error_responses_still_carry_the_headers(cors_env: None) -> None:
    """CORS is mounted outermost, so a browser sees the real status rather than
    an opaque network error -- a 404/401 must not lose the header."""
    resp = await _request(_app_with_cors(), "GET", "/no-such-route", headers={"Origin": ALLOWED})
    assert resp.status_code == 404
    assert resp.headers["access-control-allow-origin"] == ALLOWED
