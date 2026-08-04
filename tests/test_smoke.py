"""Proves the test harness itself works before it's used to pin down real bugs."""

import httpx


async def test_app_is_importable_and_client_responds(client: httpx.AsyncClient) -> None:
    resp = await client.post("/generate/qrcode", data={"content": "hello"})
    # No auth header supplied: must be rejected, not crash.
    assert resp.status_code in (401, 403)
