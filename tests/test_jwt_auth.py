"""Capability-JWT authentication: the dual-auth path (static token OR JWT), the
``?token=`` query fallback, per-endpoint scope enforcement, and the
``/upload/presign`` minting endpoint.

Static-token behaviour proper is covered by test_auth / test_token_identity;
this file exercises only what the JWT layer adds -- and, crucially, that turning
it on does not break the legacy static-token path.
"""

from __future__ import annotations

import time

import httpx
import jwt
import pytest

from app.config import settings
from app.routers.auth import (
    SCOPE_UPLOAD_IMAGE,
    SCOPE_UPLOAD_VIDEO,
    Principal,
    resolve_principal,
)
from tests.conftest import fixture_bytes

SECRET = "unit-test-jwt-secret"  # test-only signing key
STATIC = "static-master-token"  # test-only bearer secret


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable JWT auth with a known secret and a single labelled static token,
    and pin PUBLIC_BASE_URL blank so presign URL assertions are deterministic."""
    monkeypatch.setattr(settings, "FILE_MANAGER_BEARER_TOKENS", f"master:{STATIC}")
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", SECRET)
    monkeypatch.setattr(settings, "JWT_ALGORITHM", "HS256")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "")


def _mint(
    sub: str = "tenant-1",
    scopes: list[str] | None = None,
    *,
    ttl: int = 300,
    secret: str = SECRET,
    alg: str = "HS256",
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": sub,
            "scopes": [SCOPE_UPLOAD_IMAGE] if scopes is None else scopes,
            "iat": now,
            "exp": now + ttl,
        },
        secret,
        algorithm=alg,
    )


# --- resolve_principal: the pure dual-auth core ------------------------------


def test_static_token_resolves_to_unrestricted_principal(jwt_env: None) -> None:
    p = resolve_principal(STATIC)
    assert p is not None
    assert p.owner == "master"
    assert p.scopes is None  # a static master token has no scope restriction
    assert p.has_scope("upload:video") and p.has_scope("anything")


def test_valid_jwt_resolves_owner_and_scopes(jwt_env: None) -> None:
    p = resolve_principal(_mint(sub="tenant-7", scopes=[SCOPE_UPLOAD_IMAGE]))
    assert p == Principal(owner="tenant-7", scopes=frozenset({SCOPE_UPLOAD_IMAGE}))


def test_expired_jwt_is_rejected(jwt_env: None) -> None:
    assert resolve_principal(_mint(ttl=-10)) is None


def test_wrong_signature_jwt_is_rejected(jwt_env: None) -> None:
    assert resolve_principal(_mint(secret="not-the-real-secret")) is None


def test_jwt_without_any_upload_scope_is_rejected(jwt_env: None) -> None:
    # A structurally-valid token that grants no upload capability is not a
    # principal here -- it authenticates nothing this service exposes.
    assert resolve_principal(_mint(scopes=["read:files"])) is None
    assert resolve_principal(_mint(scopes=[])) is None


def test_jwt_ignored_entirely_when_secret_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "FILE_MANAGER_BEARER_TOKENS", f"master:{STATIC}")
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "")
    # Even a perfectly-formed token is refused when the feature is disabled...
    assert resolve_principal(_mint(secret="whatever")) is None
    # ...while the static token keeps working (backward compatibility).
    assert resolve_principal(STATIC) is not None


# --- Uploads: header auth, query-param auth, scope gating --------------------


async def test_image_upload_with_jwt_in_header(client: httpx.AsyncClient, jwt_env: None) -> None:
    token = _mint(sub="tenant-img", scopes=[SCOPE_UPLOAD_IMAGE])
    resp = await client.post(
        "/upload/image",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


async def test_image_upload_with_jwt_in_query_param(
    client: httpx.AsyncClient, jwt_env: None
) -> None:
    # No Authorization header at all -- the presigned-URL / header-less path.
    token = _mint(scopes=[SCOPE_UPLOAD_IMAGE])
    resp = await client.post(
        f"/upload/image?token={token}",
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    assert resp.status_code == 200


async def test_image_scoped_jwt_cannot_upload_video(
    client: httpx.AsyncClient, jwt_env: None
) -> None:
    token = _mint(scopes=[SCOPE_UPLOAD_IMAGE])
    resp = await client.post(
        "/upload/video",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("clip.mp4", fixture_bytes("tiny.mp4"), "video/mp4")},
    )
    assert resp.status_code == 403


async def test_expired_jwt_upload_is_401(client: httpx.AsyncClient, jwt_env: None) -> None:
    token = _mint(ttl=-5, scopes=[SCOPE_UPLOAD_IMAGE])
    resp = await client.post(
        "/upload/image",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    assert resp.status_code == 401


async def test_jwt_authenticates_on_a_plain_verify_token_route(
    client: httpx.AsyncClient, jwt_env: None
) -> None:
    # GET /files uses verify_token (not a scope-gated dependency); a JWT owner
    # should authenticate and see its own (empty) listing.
    token = _mint(sub="tenant-list", scopes=[SCOPE_UPLOAD_IMAGE])
    resp = await client.get("/files", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["files"] == []


# --- POST /upload/presign ----------------------------------------------------


async def test_presign_mints_a_usable_image_token(client: httpx.AsyncClient, jwt_env: None) -> None:
    resp = await client.post(
        "/upload/presign",
        headers={"Authorization": f"Bearer {STATIC}"},
        json={"kind": "image", "owner_id": "tenant-42", "expires_in_seconds": 120},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == SCOPE_UPLOAD_IMAGE
    assert body["url"].startswith("/upload/image?token=")  # PUBLIC_BASE_URL blank
    assert body["token"] in body["url"]

    # The minted URL actually authorizes a real image upload, no header needed.
    up = await client.post(
        body["url"],
        files={"file": ("tiny.png", fixture_bytes("tiny.png"), "image/png")},
    )
    assert up.status_code == 200


async def test_presign_video_uses_public_base_url_and_video_scope(
    client: httpx.AsyncClient, jwt_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://media.example.com")
    resp = await client.post(
        "/upload/presign",
        headers={"Authorization": f"Bearer {STATIC}"},
        json={"kind": "video", "owner_id": "tenant-v"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == SCOPE_UPLOAD_VIDEO
    assert body["url"].startswith("https://media.example.com/upload/video?token=")


async def test_presign_rejects_a_jwt_caller(client: httpx.AsyncClient, jwt_env: None) -> None:
    # Only the static master token may mint capabilities; a JWT holder cannot
    # escalate into issuing more (403, authenticated-but-forbidden).
    token = _mint(scopes=[SCOPE_UPLOAD_IMAGE])
    resp = await client.post(
        "/upload/presign",
        headers={"Authorization": f"Bearer {token}"},
        json={"kind": "image", "owner_id": "x"},
    )
    assert resp.status_code == 403


async def test_presign_requires_authentication(client: httpx.AsyncClient, jwt_env: None) -> None:
    resp = await client.post("/upload/presign", json={"kind": "image", "owner_id": "x"})
    assert resp.status_code == 401


async def test_presign_503_when_jwt_disabled(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "FILE_MANAGER_BEARER_TOKENS", f"master:{STATIC}")
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "")
    resp = await client.post(
        "/upload/presign",
        headers={"Authorization": f"Bearer {STATIC}"},
        json={"kind": "image", "owner_id": "x"},
    )
    assert resp.status_code == 503
