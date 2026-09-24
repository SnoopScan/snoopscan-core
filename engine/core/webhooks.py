"""Webhook delivery (01-api-surface.md, "Webhooks").

Signed with HMAC-SHA256 over the RAW body, secret per API key, sent as
`X-Signature-256: sha256=<hex>`. Receivers must verify.

Two rules that matter operationally:

  * Delivery is deduplicated on (job_id, event, page_id). A crash between the
    page write and the webhook must not send the same event twice.
  * Webhook failure NEVER fails the job. After the retry budget is spent the
    delivery is dead-lettered and logged, and the crawl carries on.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# 6 attempts, exponential backoff from 1s to 5 minutes.
RETRY_DELAYS_S = (1, 5, 25, 60, 300, 300)
MAX_ATTEMPTS = len(RETRY_DELAYS_S)


@dataclass
class WebhookEvent:
    event: str
    job_id: str
    data: dict[str, Any] = field(default_factory=dict)
    page_id: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "jobId": self.job_id,
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data": self.data,
        }

    def dedup_key(self) -> str:
        return f"webhook:sent:{self.job_id}:{self.event}:{self.page_id or '-'}"


def sign(body: bytes, secret: str) -> str:
    """HMAC-SHA256 over the raw body — not over a re-serialised dict.

    Re-serialising changes key order and whitespace, so a receiver computing
    the digest over the bytes it received would disagree with us.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify(body: bytes, secret: str, provided: str) -> bool:
    """Constant-time comparison, for receivers and for our own tests."""
    return hmac.compare_digest(sign(body, secret), provided)


class WebhookSender:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        return self._client

    async def deliver(
        self,
        url: str,
        event: WebhookEvent,
        *,
        secret: str | None = None,
        extra_headers: dict[str, str] | None = None,
        attempt: int = 0,
    ) -> bool:
        """One delivery attempt. Returns True on a 2xx.

        The caller schedules retries using `RETRY_DELAYS_S`; this method does
        not sleep, so a worker is never blocked on a slow receiver.
        """
        body = json.dumps(event.payload(), separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        if secret:
            headers["X-Signature-256"] = sign(body, secret)

        client = await self._get_client()
        try:
            response = await client.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "webhook_delivery_error",
                job_id=event.job_id,
                webhook_event=event.event,
                attempt=attempt,
                error=str(exc),
            )
            return False

        if 200 <= response.status_code < 300:
            return True

        logger.warning(
            "webhook_non_2xx",
            job_id=event.job_id,
            webhook_event=event.event,
            attempt=attempt,
            status=response.status_code,
        )
        return False

    async def deliver_with_retries(
        self,
        url: str,
        event: WebhookEvent,
        *,
        secret: str | None = None,
        extra_headers: dict[str, str] | None = None,
        sleep: Any = None,
    ) -> bool:
        """Full retry budget. After it is spent the delivery is dead-lettered
        and logged — and the job still succeeds."""
        import asyncio

        sleeper = sleep or asyncio.sleep
        for attempt in range(MAX_ATTEMPTS):
            if await self.deliver(
                url, event, secret=secret, extra_headers=extra_headers, attempt=attempt
            ):
                return True
            if attempt < MAX_ATTEMPTS - 1:
                await sleeper(RETRY_DELAYS_S[attempt])

        logger.error(
            "webhook_dead_lettered",
            job_id=event.job_id,
            webhook_event=event.event,
            url_host=httpx.URL(url).host,
            attempts=MAX_ATTEMPTS,
        )
        return False

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


async def send_once(
    sender: WebhookSender,
    redis: Any,
    url: str,
    event: WebhookEvent,
    *,
    secret: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> bool:
    """Deduplicated delivery.

    The dedup key is set BEFORE sending and only cleared on failure, so a
    crash mid-send fails closed (no duplicate) rather than open.
    """
    key = event.dedup_key()
    claimed = await redis.set(key, "1", nx=True, ex=86_400)
    if not claimed:
        logger.debug("webhook_already_sent", job_id=event.job_id, webhook_event=event.event)
        return True

    delivered = await sender.deliver_with_retries(
        url, event, secret=secret, extra_headers=extra_headers
    )
    if not delivered:
        await redis.delete(key)
    return delivered
