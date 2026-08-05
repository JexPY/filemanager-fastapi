from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

import app.services.metadata as metadata_module
import app.services.storage as storage_module
from app.broker import broker
from app.config import settings
from app.main import app
from tests.fakes import InMemoryMetadataStore, InMemoryStorageBackend

FIXTURES_DIR = Path(__file__).parent / "fixtures"
TEST_TOKEN = "test-token"


@pytest.fixture
def fake_storage(monkeypatch: pytest.MonkeyPatch) -> InMemoryStorageBackend:
    """Bypasses get_storage()'s lazy singleton build entirely by pre-seeding it,
    so upload_file/download_file/delete_file (imported by name into files.py and
    tasks.py) transparently operate on the fake regardless of who imported them.
    """
    backend = InMemoryStorageBackend()
    monkeypatch.setattr(storage_module, "_storage", backend)
    return backend


@pytest.fixture
def fake_metadata(monkeypatch: pytest.MonkeyPatch) -> InMemoryMetadataStore:
    """Pre-seeds the module-level metadata singleton the same way fake_storage
    does for storage, so get_metadata_store() returns this fake in every module
    that imported it -- no live Postgres needed for route/unit tests.
    """
    store = InMemoryMetadataStore()
    monkeypatch.setattr(metadata_module, "_store", store)
    return store


@pytest.fixture
def auth_headers(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(settings, "FILE_MANAGER_BEARER_TOKENS", TEST_TOKEN)
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
async def client(
    fake_storage: InMemoryStorageBackend, fake_metadata: InMemoryMetadataStore
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@dataclass
class FakeTaskResult:
    is_err: bool
    error: Exception | None = None
    return_value: Any = None


class FakeResultBackend:
    """Stands in for broker.result_backend (a RedisAsyncResultBackend) so
    GET /tasks/{id} tests never need a live Redis.
    """

    def __init__(self) -> None:
        self._results: dict[str, FakeTaskResult] = {}

    def set_result(self, task_id: str, result: FakeTaskResult) -> None:
        self._results[task_id] = result

    async def is_result_ready(self, task_id: str) -> bool:
        return task_id in self._results

    async def get_result(self, task_id: str) -> FakeTaskResult:
        return self._results[task_id]


@pytest.fixture
def fake_result_backend(monkeypatch: pytest.MonkeyPatch) -> FakeResultBackend:
    fake = FakeResultBackend()
    # `broker` is the same object everywhere it's imported, so patching this
    # attribute affects app.routers.files' `broker` reference too.
    monkeypatch.setattr(broker, "result_backend", fake)
    return fake


@pytest.fixture
def fake_enqueue(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Avoids compress_video_task.kiq(...) hitting a real Redis stream."""
    from app.tasks import compress_video_task

    calls: list[dict[str, Any]] = []

    class _FakeEnqueuedTask:
        task_id = "fake-task-id-0000"

    async def fake_kiq(**kwargs: Any) -> _FakeEnqueuedTask:
        calls.append(kwargs)
        return _FakeEnqueuedTask()

    monkeypatch.setattr(compress_video_task, "kiq", fake_kiq)
    return calls


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()
