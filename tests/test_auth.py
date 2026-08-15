import httpx
import pytest

from app.config import Settings


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
