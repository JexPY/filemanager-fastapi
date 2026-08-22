import httpx
import pytest

from app.config import Settings, _derive_owner, settings


async def test_missing_auth_header_is_401(client: httpx.AsyncClient) -> None:
    resp = await client.post("/generate/qrcode", data={"content": "hi"})
    assert resp.status_code == 401


async def test_malformed_auth_scheme_is_401(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/generate/qrcode", data={"content": "hi"}, headers={"Authorization": "Basic abc123"}
    )
    assert resp.status_code == 401


async def test_wrong_token_is_401(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    resp = await client.post(
        "/generate/qrcode",
        data={"content": "hi"},
        headers={"Authorization": "Bearer not-the-right-token"},
    )
    assert resp.status_code == 401


async def test_correct_token_passes_auth(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post("/generate/qrcode", data={"content": "hi"}, headers=auth_headers)
    assert resp.status_code != 401


def test_empty_bearer_tokens_fails_fast() -> None:
    # Explicit init kwargs take priority over env vars/.env in pydantic-settings,
    # so this exercises the validator regardless of the ambient test environment.
    with pytest.raises(ValueError, match="FILE_MANAGER_BEARER_TOKENS"):
        Settings(FILE_MANAGER_BEARER_TOKENS="")


async def test_whoami_with_labelled_static_token(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "FILE_MANAGER_BEARER_TOKENS", "fixcar:secret-token-123")
    resp = await client.get("/whoami", headers={"Authorization": "Bearer secret-token-123"})
    assert resp.status_code == 200
    assert resp.json() == {"owner": "fixcar"}


async def test_whoami_with_unlabelled_static_token(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "FILE_MANAGER_BEARER_TOKENS", "baresecret")
    resp = await client.get("/whoami", headers={"Authorization": "Bearer baresecret"})
    assert resp.status_code == 200
    assert resp.json() == {"owner": _derive_owner("baresecret")}


async def test_whoami_unauthenticated_is_401(client: httpx.AsyncClient) -> None:
    resp = await client.get("/whoami")
    assert resp.status_code == 401


async def test_whoami_wrong_token_is_401(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "FILE_MANAGER_BEARER_TOKENS", "fixcar:secret-token-123")
    resp = await client.get("/whoami", headers={"Authorization": "Bearer invalid-token"})
    assert resp.status_code == 401
