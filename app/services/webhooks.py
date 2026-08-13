"""Signed, SSRF-guarded webhook delivery for video-compression completion.

A client may pass a `callback_url` on POST /upload/video. When the worker
finishes (success or failure) it POSTs a signed JSON payload there so receivers
don't have to poll GET /tasks/{id}.

Two independent concerns live here:

* **Admission** (`validate_callback_url`, runs in the api at upload time): the
  URL must use an allowed scheme (https unless WEBHOOK_ALLOW_INSECURE_HTTP), its
  host must be on the explicit WEBHOOK_ALLOWED_HOSTS allow-list, and -- unless
  WEBHOOK_ALLOW_PRIVATE_IPS -- must not resolve to a private/loopback/link-local
  address. The allow-list is the authoritative egress control (it's what makes
  the worker's outbound POST safe against SSRF); the IP check is defence in depth.
  Rejections raise `WebhookValidationError` with a client-safe message -> 400.

* **Delivery** (`deliver_webhook`, runs in the worker's own webhook task): HMAC-
  SHA256 signs the body, POSTs with retries + exponential backoff, and is
  entirely best-effort -- it never raises, so a dead webhook can't fail or mask
  the compression result. It returns a WebhookDeliveryResult; the delivery task
  persists that outcome on the uploads row (webhook_status/attempts/last_error),
  so an exhausted delivery is a durable dead-letter record that the owner can
  replay via POST /files/{id}/redeliver -- not just a log line.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import aiohttp

from app.config import settings

logger = logging.getLogger(__name__)


class WebhookValidationError(ValueError):
    """A client-supplied callback_url that fails admission. Message is client-safe."""


@dataclass(frozen=True)
class WebhookDeliveryResult:
    """Outcome of one delivery cycle, so the caller can persist a dead-letter
    record: whether a 2xx was seen, how many attempts it took, and a short
    reason for the last failure (None on success)."""

    delivered: bool
    attempts: int
    last_error: str | None


def _reject_if_private(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise WebhookValidationError("callback_url host could not be resolved") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise WebhookValidationError("callback_url host resolves to a disallowed address")


def validate_callback_url(url: str) -> str:
    """Admit a client-supplied callback URL or raise WebhookValidationError.

    Called in the api at upload time so the client gets immediate 400 feedback
    rather than a silent non-delivery later.
    """
    if not settings.webhooks_enabled:
        raise WebhookValidationError("Webhooks are not enabled on this server")

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    allowed_schemes = {"https"}
    if settings.WEBHOOK_ALLOW_INSECURE_HTTP:
        allowed_schemes.add("http")
    if scheme not in allowed_schemes:
        raise WebhookValidationError("callback_url must use https")

    host = parsed.hostname
    if not host:
        raise WebhookValidationError("callback_url is missing a host")
    if host.lower() not in settings.parsed_webhook_allowed_hosts:
        raise WebhookValidationError("callback_url host is not allowed")

    if not settings.WEBHOOK_ALLOW_PRIVATE_IPS:
        _reject_if_private(host)
    return url


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 over `timestamp.body`, so a captured body can't be replayed
    under a new timestamp. Returned as `sha256=<hex>`."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def _backoff_seconds(attempt: int) -> float:
    # attempt is 1-based; delay grows exponentially from the configured base and
    # is capped so a large WEBHOOK_MAX_ATTEMPTS can't wedge a worker slot for long.
    return min(settings.WEBHOOK_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)), 30.0)


async def deliver_webhook(
    *, url: str, event: str, data: dict[str, Any], delivery_id: str
) -> WebhookDeliveryResult:
    """Best-effort signed POST to `url`. Returns a WebhookDeliveryResult
    (delivered on a 2xx, else exhausted). Never raises -- callers can ignore
    the result or persist it as a dead-letter record."""
    payload = {
        "id": delivery_id,
        "event": event,
        "created_at": datetime.now(UTC).isoformat(),
        "data": data,
    }
    # Compact, stable serialization so the bytes the receiver verifies are the
    # exact bytes we signed.
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "filemanager-fastapi-webhooks/1",
        "X-Webhook-Id": delivery_id,  # idempotency key, stable across retries
        "X-Webhook-Event": event,
    }
    timeout = aiohttp.ClientTimeout(total=settings.WEBHOOK_TIMEOUT_SECONDS)

    last_error = "no attempt made"
    attempt = 0
    async with aiohttp.ClientSession() as session:
        for attempt in range(1, settings.WEBHOOK_MAX_ATTEMPTS + 1):
            timestamp = str(int(time.time()))
            headers["X-Webhook-Timestamp"] = timestamp
            headers["X-Webhook-Signature"] = _sign(settings.WEBHOOK_SIGNING_SECRET, timestamp, body)
            try:
                async with session.post(url, data=body, headers=headers, timeout=timeout) as resp:
                    if 200 <= resp.status < 300:
                        logger.info(
                            "Webhook %s delivered to %s (attempt %d, status %d)",
                            delivery_id,
                            url,
                            attempt,
                            resp.status,
                        )
                        return WebhookDeliveryResult(
                            delivered=True, attempts=attempt, last_error=None
                        )
                    last_error = f"HTTP {resp.status}"
                    logger.warning(
                        "Webhook %s got non-2xx from %s (attempt %d, status %d)",
                        delivery_id,
                        url,
                        attempt,
                        resp.status,
                    )
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "Webhook %s delivery to %s failed (attempt %d): %s",
                    delivery_id,
                    url,
                    attempt,
                    exc,
                )
            if attempt < settings.WEBHOOK_MAX_ATTEMPTS:
                await asyncio.sleep(_backoff_seconds(attempt))

    logger.error(
        "Webhook %s exhausted %d attempts to %s; dead-lettered (last error: %s)",
        delivery_id,
        settings.WEBHOOK_MAX_ATTEMPTS,
        url,
        last_error,
    )
    return WebhookDeliveryResult(delivered=False, attempts=attempt, last_error=last_error)
