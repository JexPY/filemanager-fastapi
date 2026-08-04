import httpx
import pytest

import app.routers.health as health_module


async def test_healthz_always_returns_200(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readyz_200_when_redis_and_storage_reachable(client: httpx.AsyncClient) -> None:
    resp = await client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"] == {"redis": "ok", "storage": "ok"}


async def test_readyz_503_when_redis_unreachable(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_check_redis() -> bool:
        return False

    monkeypatch.setattr(health_module, "_check_redis", fake_check_redis)
    resp = await client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["redis"] == "unavailable"


async def test_readyz_503_when_storage_unreachable(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_check_storage() -> bool:
        return False

    monkeypatch.setattr(health_module, "_check_storage", fake_check_storage)
    resp = await client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["storage"] == "unavailable"


async def test_response_includes_generated_request_id(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert "X-Request-ID" in resp.headers
    assert len(resp.headers["X-Request-ID"]) > 0


async def test_caller_supplied_request_id_is_echoed_back(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz", headers={"X-Request-ID": "my-custom-id-123"})
    assert resp.headers["X-Request-ID"] == "my-custom-id-123"
