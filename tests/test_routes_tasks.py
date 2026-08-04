import uuid
from typing import Any

import httpx

from app.services.task_status import mark_task_issued
from tests.conftest import FakeResultBackend, FakeTaskResult


async def test_unknown_task_id_returns_404(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_result_backend: FakeResultBackend,
) -> None:
    bogus_id = uuid.uuid4().hex  # never enqueued, never marked issued
    resp = await client.get(f"/tasks/{bogus_id}", headers=auth_headers)
    assert resp.status_code == 404


async def test_issued_but_unfinished_task_is_pending(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_result_backend: FakeResultBackend,
) -> None:
    task_id = uuid.uuid4().hex
    await mark_task_issued(task_id)

    resp = await client.get(f"/tasks/{task_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"task_id": task_id, "status": "pending"}


async def test_video_upload_marks_task_issued_so_status_is_pending_not_404(
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
    fake_result_backend: FakeResultBackend,
) -> None:
    task_id = uuid.uuid4().hex
    fake_result_backend.set_result(
        task_id, FakeTaskResult(is_err=False, return_value={"status": "success", "key": "x"})
    )

    resp = await client.get(f"/tasks/{task_id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["result"] == {"status": "success", "key": "x"}


async def test_failed_task_returns_sanitized_error(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_result_backend: FakeResultBackend,
) -> None:
    task_id = uuid.uuid4().hex
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
