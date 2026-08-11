import uuid
from typing import Any

import httpx
import pytest

from app.config import _derive_owner, settings
from app.services.metadata import KIND_VIDEO, STATUS_PROCESSING
from tests.conftest import FakeResultBackend, FakeTaskResult
from tests.fakes import InMemoryMetadataStore

OWNER = _derive_owner("test-token")


async def _seed_task(store: InMemoryMetadataStore, task_id: str, owner: str = OWNER) -> None:
    """A video upload row linked to `task_id` -- the durable proof the task was
    issued and whose owner it belongs to (GET /tasks/{id} scopes off this)."""
    record = await store.create(
        owner=owner,
        kind=KIND_VIDEO,
        storage_key="raw/videos/x.mp4",
        content_type="video/mp4",
        size_bytes=1,
        status=STATUS_PROCESSING,
    )
    await store.set_task_id(record.id, task_id)


async def test_unknown_task_id_returns_404(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_result_backend: FakeResultBackend,
) -> None:
    bogus_id = uuid.uuid4().hex  # no record links to it
    resp = await client.get(f"/tasks/{bogus_id}", headers=auth_headers)
    assert resp.status_code == 404


async def test_another_owners_task_is_404_not_leaked(
    client: httpx.AsyncClient,
    fake_metadata: InMemoryMetadataStore,
    fake_result_backend: FakeResultBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "FILE_MANAGER_BEARER_TOKENS", "alice:tok-a,bob:tok-b")
    task_id = uuid.uuid4().hex
    await _seed_task(fake_metadata, task_id, owner="alice")
    # Even with a ready result in the backend, bob must not see alice's task.
    fake_result_backend.set_result(task_id, FakeTaskResult(is_err=False, return_value={"ok": 1}))

    resp = await client.get(f"/tasks/{task_id}", headers={"Authorization": "Bearer tok-b"})
    assert resp.status_code == 404
    # The real owner still sees it.
    ok = await client.get(f"/tasks/{task_id}", headers={"Authorization": "Bearer tok-a"})
    assert ok.status_code == 200


async def test_issued_but_unfinished_task_is_pending(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_result_backend: FakeResultBackend,
) -> None:
    task_id = uuid.uuid4().hex
    await _seed_task(fake_metadata, task_id)

    resp = await client.get(f"/tasks/{task_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"task_id": task_id, "status": "pending"}


async def test_video_upload_makes_task_pollable_as_pending_not_404(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_enqueue: list[dict[str, Any]],
    fake_result_backend: FakeResultBackend,
) -> None:
    from tests.conftest import fixture_bytes

    upload_resp = await client.post(
        "/upload/video",
        headers=auth_headers,
        files={"file": ("tiny.mp4", fixture_bytes("tiny.mp4"), "video/mp4")},
    )
    task_id = upload_resp.json()["task_id"]

    status_resp = await client.get(f"/tasks/{task_id}", headers=auth_headers)
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "pending"


async def test_completed_task_returns_result(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_result_backend: FakeResultBackend,
) -> None:
    task_id = uuid.uuid4().hex
    await _seed_task(fake_metadata, task_id)
    fake_result_backend.set_result(
        task_id,
        FakeTaskResult(is_err=False, return_value={"status": "success", "upload_id": task_id}),
    )

    resp = await client.get(f"/tasks/{task_id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    # Thin task result: the route passes through only execution state. The
    # client then fetches domain data live via GET /files/{upload_id}.
    assert body["result"] == {"status": "success", "upload_id": task_id}


async def test_failed_task_returns_sanitized_error(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    fake_result_backend: FakeResultBackend,
) -> None:
    task_id = uuid.uuid4().hex
    await _seed_task(fake_metadata, task_id)
    fake_result_backend.set_result(
        task_id,
        FakeTaskResult(
            is_err=True,
            error=RuntimeError("FFmpeg failed: [internal] /tmp/abc123_raw.mp4: some codec detail"),
        ),
    )

    resp = await client.get(f"/tasks/{task_id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error"] == "Video processing failed"
    assert "/tmp/" not in body["error"]
    assert "ffmpeg" not in body["error"].lower()
