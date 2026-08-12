"""Correlation IDs across the API -> Redis -> worker boundary.

Stdlib logging only, no new dependency: a per-request id is generated (or
reused from an inbound X-Request-ID header), stashed in a ContextVar for the
duration of the request, echoed back on the response, and injected into
every log record via a logging.Filter so log lines from the same request
can be grepped together.
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIDLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class RequestIDMiddleware:
    """Pure ASGI middleware -- no BaseHTTPMiddleware wrapper, so streaming
    requests (large uploads) and responses are never buffered or interrupted
    by a CancelScope."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Pull X-Request-ID from headers or generate one.
        headers = dict(scope.get("headers", []))
        request_id = headers.get(b"x-request-id", b"").decode() or uuid.uuid4().hex
        token = request_id_var.set(request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.append("X-Request-ID", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            request_id_var.reset(token)
