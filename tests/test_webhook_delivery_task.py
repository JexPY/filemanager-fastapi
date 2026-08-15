"""Webhook delivery as its own TaskIQ task (decoupled from compression) plus the
owner-scoped manual redelivery route.

The delivery task re-loads the record, signs+POSTs it, and persists the outcome
on the row as a durable dead-letter record. Here the low-level deliver_webhook
(the HTTP hop, covered in test_webhooks.py) is stubbed so we can assert the
task's *bookkeeping* deterministically; the redelivery route is exercised over
the ASGI transport.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import app.tasks as tasks_module
from app.config import _derive_owner, settings
from app.services.metadata import (
    KIND_VIDEO,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_READY,
    WEBHOOK_DELIVERED,
    WEBHOOK_FAILED,
)
from app.services.webhooks import WebhookDeliveryResult
from app.tasks import deliver_webhook_task
from tests.fakes import InMemoryMetadataStore

OWNER = _derive_owner("test-token")


async def _video(
    store: InMemoryMetadataStore, *, status: str = STATUS_READY, callback_url: str | None = None
) -> str:
    record = await store.create(
        owner=OWNER,
        kind=KIND_VIDEO,
        storage_key="videos/x_compressed.mp4",
        content_type="video/mp4",
        size_bytes=1,
        status=status,
        callback_url=callback_url,
    )
    return record.id


def _stub_deliver(
    monkeypatch: pytest.MonkeyPatch, result: WebhookDeliveryResult
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_deliver(**kwargs: Any) -> WebhookDeliveryResult:
        calls.append(kwargs)
        return result

    monkeypatch.setattr(tasks_module, "deliver_webhook", fake_deliver)
    return calls


# --- The delivery task's bookkeeping --------------------------------------


async def test_task_records_delivered_state(
    fake_metadata: InMemoryMetadataStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_id = await _video(fake_metadata, callback_url="https://h/hook")
    _stub_deliver(monkeypatch, WebhookDeliveryResult(delivered=True, attempts=1, last_error=None))

    out = await deliver_webhook_task(upload_id=upload_id, event="video.completed")

    assert out["status"] == WEBHOOK_DELIVERED
    record = await fake_metadata.get(upload_id, OWNER)
    assert record is not None
    assert record.webhook_status == WEBHOOK_DELIVERED
    assert record.webhook_attempts == 1
    assert record.webhook_last_error is None


async def test_task_records_failed_deadletter(
    fake_metadata: InMemoryMetadataStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_id = await _video(fake_metadata, callback_url="https://h/hook")
    _stub_deliver(
        monkeypatch, WebhookDeliveryResult(delivered=False, attempts=4, last_error="HTTP 500")
    )

    out = await deliver_webhook_task(upload_id=upload_id, event="video.failed")

    assert out["status"] == WEBHOOK_FAILED
    record = await fake_metadata.get(upload_id, OWNER)
    assert record is not None
    assert record.webhook_status == WEBHOOK_FAILED
    assert record.webhook_attempts == 4
    assert record.webhook_last_error == "HTTP 500"


async def test_task_signs_current_record_state(
    fake_metadata: InMemoryMetadataStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_id = await _video(fake_metadata, callback_url="https://h/hook")
    calls = _stub_deliver(
        monkeypatch, WebhookDeliveryResult(delivered=True, attempts=1, last_error=None)
    )

    await deliver_webhook_task(upload_id=upload_id, event="video.completed")

    assert len(calls) == 1
    assert calls[0]["url"] == "https://h/hook"
    assert calls[0]["delivery_id"] == upload_id  # stable idempotency key
    assert calls[0]["event"] == "video.completed"
    assert calls[0]["data"]["id"] == upload_id
    assert calls[0]["data"]["status"] == STATUS_READY


async def test_task_skips_when_no_callback(
    fake_metadata: InMemoryMetadataStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_id = await _video(fake_metadata)  # no callback_url
    calls = _stub_deliver(
        monkeypatch, WebhookDeliveryResult(delivered=True, attempts=1, last_error=None)
    )

    out = await deliver_webhook_task(upload_id=upload_id, event="video.completed")

    assert out["status"] == "skipped"
    assert calls == []


# --- The redelivery route --------------------------------------------------


@pytest.fixture
def webhooks_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WEBHOOK_SIGNING_SECRET", "s")
    monkeypatch.setattr(settings, "WEBHOOK_ALLOWED_HOSTS", "h")


@pytest.fixture
def redeliver_enqueue(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class _FakeTask:
        task_id = "redeliver-task-0"

    async def fake_kiq(**kwargs: Any) -> _FakeTask:
        calls.append(kwargs)
        return _FakeTask()

    monkeypatch.setattr(deliver_webhook_task, "kiq", fake_kiq)
    return calls


async def test_redeliver_enqueues_completed_for_ready_video(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    redeliver_enqueue: list[dict[str, Any]],
    webhooks_configured: None,
) -> None:
    upload_id = await _video(fake_metadata, callback_url="https://h/hook")

    resp = await client.post(f"/files/{upload_id}/redeliver", headers=auth_headers)

    assert resp.status_code == 202
    assert resp.json()["event"] == "video.completed"
    assert redeliver_enqueue[0]["upload_id"] == upload_id
    assert redeliver_enqueue[0]["event"] == "video.completed"


async def test_redeliver_enqueues_failed_for_failed_video(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    redeliver_enqueue: list[dict[str, Any]],
    webhooks_configured: None,
) -> None:
    upload_id = await _video(fake_metadata, status=STATUS_FAILED, callback_url="https://h/hook")

    resp = await client.post(f"/files/{upload_id}/redeliver", headers=auth_headers)

    assert resp.status_code == 202
    assert resp.json()["event"] == "video.failed"
    assert redeliver_enqueue[0]["event"] == "video.failed"


async def test_redeliver_404_for_unknown(
    client: httpx.AsyncClient, auth_headers: dict[str, str], webhooks_configured: None
) -> None:
    resp = await client.post("/files/nope/redeliver", headers=auth_headers)
    assert resp.status_code == 404


async def test_redeliver_400_without_callback(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    webhooks_configured: None,
) -> None:
    upload_id = await _video(fake_metadata)  # no callback_url
    resp = await client.post(f"/files/{upload_id}/redeliver", headers=auth_headers)
    assert resp.status_code == 400


async def test_redeliver_409_while_processing(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    webhooks_configured: None,
) -> None:
    upload_id = await _video(fake_metadata, status=STATUS_PROCESSING, callback_url="https://h/hook")
    resp = await client.post(f"/files/{upload_id}/redeliver", headers=auth_headers)
    assert resp.status_code == 409
