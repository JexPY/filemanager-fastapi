"""GET /files/batch: owner-scoped lookup of many records by id in one call.

Covers the actual behavior a listing page needs (many photos, one round
trip), the "existence never leaks" silent-omission rule, the id-count cap,
and -- critically -- that this route is not shadowed by GET /files/{file_id}
(Starlette matches routes in registration order; batch must be registered
first, see app.main).
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.config import _derive_owner
from app.services.metadata import KIND_IMAGE, STATUS_READY
from tests.fakes import InMemoryMetadataStore

OWNER = _derive_owner("test-token")
OTHER_OWNER = _derive_owner("other-owner-token")


async def _image(store: InMemoryMetadataStore, owner: str, key: str) -> str:
    record = await store.create(
        owner=owner,
        kind=KIND_IMAGE,
        storage_key=key,
        content_type="image/webp",
        size_bytes=10,
        status=STATUS_READY,
        visibility="public",
    )
    return record.id


async def test_batch_returns_only_the_requested_owners_records(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    a = await _image(fake_metadata, OWNER, "images/a.webp")
    b = await _image(fake_metadata, OWNER, "images/b.webp")
    foreign = await _image(fake_metadata, OTHER_OWNER, "images/foreign.webp")

    resp = await client.post(
        "/files/batch", headers=auth_headers, json={"ids": [a, b, foreign, "does-not-exist"]}
    )

    assert resp.status_code == 200
    body = resp.json()
    returned_ids = {f["id"] for f in body["files"]}
    # Unknown id and another owner's id are silently dropped, not an error --
    # existence never leaks, same as every other owner-scoped route.
    assert returned_ids == {a, b}
    assert len(body["files"]) == 2


async def test_batch_with_no_matches_returns_empty_list_not_an_error(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    resp = await client.post("/files/batch", headers=auth_headers, json={"ids": ["nope-1"]})
    assert resp.status_code == 200
    assert resp.json()["files"] == []


async def test_batch_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.post("/files/batch", json={"ids": ["whatever"]})
    assert resp.status_code == 401


async def test_batch_rejects_more_than_200_ids(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    ids = [f"id-{i}" for i in range(201)]
    resp = await client.post("/files/batch", headers=auth_headers, json={"ids": ids})
    assert resp.status_code == 422


async def test_batch_route_is_not_shadowed_by_file_id_route(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
) -> None:
    """Regression guard: if /files/{file_id} were registered before
    /files/batch, a literal request to /files/batch would be swallowed as
    file_id='batch' and 404 (no record has that id) instead of hitting this
    route and returning the batch-list shape."""
    known = await _image(fake_metadata, OWNER, "images/known.webp")

    resp = await client.post("/files/batch", headers=auth_headers, json={"ids": [known]})

    assert resp.status_code == 200
    assert "files" in resp.json()


async def test_batch_partial_miss_logs_warning(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_metadata: InMemoryMetadataStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    a = await _image(fake_metadata, OWNER, "images/a.webp")

    with caplog.at_level(logging.WARNING):
        resp = await client.post(
            "/files/batch",
            headers=auth_headers,
            json={"ids": [a, "missing-1", "missing-2"]},
        )
    assert resp.status_code == 200
    assert len(resp.json()["files"]) == 1
    assert resp.json()["files"][0]["id"] == a

    warning_logs = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert any(
        f"Batch lookup partial miss: owner={OWNER} requested=3 returned=1" in rec.message
        for rec in warning_logs
    )
