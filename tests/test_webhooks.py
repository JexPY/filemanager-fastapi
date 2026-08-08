"""Webhook admission (SSRF/allow-list), signed delivery + retries, and the
upload route's callback_url validation.

Delivery tests run a real aiohttp server on 127.0.0.1 so the full signed POST
path is exercised end to end (no mocking of the HTTP client).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from aiohttp import web

from app.config import _derive_owner, settings
from app.services.webhooks import (
    WebhookValidationError,
    deliver_webhook,
    validate_callback_url,
)
from tests.conftest import fixture_bytes
from tests.fakes import InMemoryMetadataStore

OWNER = _derive_owner("test-token")


@pytest.fixture
def webhooks_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable webhooks pointed at an in-network 127.0.0.1 receiver (private IPs
    allowed, http permitted) -- the shape a local/dev deployment would use."""
    monkeypatch.setattr(settings, "WEBHOOK_SIGNING_SECRET", "shh-secret")
    monkeypatch.setattr(settings, "WEBHOOK_ALLOWED_HOSTS", "127.0.0.1,localhost")
    monkeypatch.setattr(settings, "WEBHOOK_ALLOW_INSECURE_HTTP", True)
    monkeypatch.setattr(settings, "WEBHOOK_ALLOW_PRIVATE_IPS", True)
    monkeypatch.setattr(settings, "WEBHOOK_RETRY_BACKOFF_SECONDS", 0.0)


@asynccontextmanager
async def _receiver(handler: Any) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_post("/hook", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}/hook"
    finally:
        await runner.cleanup()


# --- Admission -------------------------------------------------------------


def test_validate_rejects_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WEBHOOK_SIGNING_SECRET", "")
    monkeypatch.setattr(settings, "WEBHOOK_ALLOWED_HOSTS", "")
    with pytest.raises(WebhookValidationError, match="not enabled"):
        validate_callback_url("https://hooks.example.com/x")


def test_validate_requires_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WEBHOOK_SIGNING_SECRET", "s")
    monkeypatch.setattr(settings, "WEBHOOK_ALLOWED_HOSTS", "hooks.example.com")
    monkeypatch.setattr(settings, "WEBHOOK_ALLOW_INSECURE_HTTP", False)
    with pytest.raises(WebhookValidationError, match="https"):
        validate_callback_url("http://hooks.example.com/x")


def test_validate_rejects_host_not_on_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WEBHOOK_SIGNING_SECRET", "s")
    monkeypatch.setattr(settings, "WEBHOOK_ALLOWED_HOSTS", "hooks.example.com")
    with pytest.raises(WebhookValidationError, match="not allowed"):
        validate_callback_url("https://evil.example.net/x")


def test_validate_blocks_private_ip_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # localhost is on the allow-list but resolves to a loopback address; with the
    # private-IP guard on (the default) that must be refused -- the SSRF case.
    monkeypatch.setattr(settings, "WEBHOOK_SIGNING_SECRET", "s")
    monkeypatch.setattr(settings, "WEBHOOK_ALLOWED_HOSTS", "localhost")
    monkeypatch.setattr(settings, "WEBHOOK_ALLOW_INSECURE_HTTP", True)
    monkeypatch.setattr(settings, "WEBHOOK_ALLOW_PRIVATE_IPS", False)
    with pytest.raises(WebhookValidationError, match="disallowed address"):
        validate_callback_url("http://localhost/x")


def test_validate_accepts_allowed_host(webhooks_on: None) -> None:
    assert validate_callback_url("http://localhost/hook") == "http://localhost/hook"


# --- Signed delivery -------------------------------------------------------


async def test_deliver_signs_and_posts(webhooks_on: None) -> None:
    seen: dict[str, Any] = {}

    async def handler(request: web.Request) -> web.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = await request.read()
        return web.Response(status=200)

    async with _receiver(handler) as url:
        ok = await deliver_webhook(
            url=url, event="video.completed", data={"id": "u1"}, delivery_id="u1"
        )

    assert ok is True
    ts = seen["headers"]["X-Webhook-Timestamp"]
    expected = (
        "sha256="
        + hmac.new(b"shh-secret", f"{ts}.".encode() + seen["body"], hashlib.sha256).hexdigest()
    )
    assert seen["headers"]["X-Webhook-Signature"] == expected
    assert seen["headers"]["X-Webhook-Id"] == "u1"
    assert seen["headers"]["X-Webhook-Event"] == "video.completed"


async def test_deliver_retries_then_succeeds(webhooks_on: None) -> None:
    attempts = {"n": 0}

    async def handler(request: web.Request) -> web.Response:
        attempts["n"] += 1
        return web.Response(status=200 if attempts["n"] >= 3 else 500)

    async with _receiver(handler) as url:
        ok = await deliver_webhook(url=url, event="video.completed", data={}, delivery_id="u2")

    assert ok is True
    assert attempts["n"] == 3


async def test_deliver_gives_up_after_max_attempts(
    webhooks_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "WEBHOOK_MAX_ATTEMPTS", 2)
    attempts = {"n": 0}

    async def handler(request: web.Request) -> web.Response:
        attempts["n"] += 1
        return web.Response(status=500)

    async with _receiver(handler) as url:
        ok = await deliver_webhook(url=url, event="video.failed", data={}, delivery_id="u3")

    assert ok is False
    assert attempts["n"] == 2


# --- Upload route validation ----------------------------------------------


async def test_upload_stores_valid_callback_url(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_enqueue: list[dict[str, Any]],
    webhooks_on: None,
) -> None:
    resp = await client.post(
        "/upload/video",
        headers=auth_headers,
        files={"file": ("tiny.mp4", fixture_bytes("tiny.mp4"), "video/mp4")},
        data={"callback_url": "http://localhost:9999/hook"},
    )
    assert resp.status_code == 202
    record = await fake_metadata.get(resp.json()["id"], OWNER)
    assert record is not None
    assert record.callback_url == "http://localhost:9999/hook"


async def test_upload_rejects_disallowed_callback_host(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_enqueue: list[dict[str, Any]],
    webhooks_on: None,
) -> None:
    resp = await client.post(
        "/upload/video",
        headers=auth_headers,
        files={"file": ("tiny.mp4", fixture_bytes("tiny.mp4"), "video/mp4")},
        data={"callback_url": "https://evil.example.net/hook"},
    )
    assert resp.status_code == 400
    assert len(fake_enqueue) == 0  # nothing staged or enqueued


async def test_upload_rejects_callback_when_webhooks_disabled(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_enqueue: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "WEBHOOK_SIGNING_SECRET", "")
    monkeypatch.setattr(settings, "WEBHOOK_ALLOWED_HOSTS", "")
    resp = await client.post(
        "/upload/video",
        headers=auth_headers,
        files={"file": ("tiny.mp4", fixture_bytes("tiny.mp4"), "video/mp4")},
        data={"callback_url": "https://hooks.example.com/hook"},
    )
    assert resp.status_code == 400
