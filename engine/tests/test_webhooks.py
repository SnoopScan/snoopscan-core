"""Webhook signing, retry and deduplication."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from engine.core.webhooks import (
    MAX_ATTEMPTS,
    WebhookEvent,
    WebhookSender,
    send_once,
    sign,
    verify,
)


def event(name: str = "completed") -> WebhookEvent:
    return WebhookEvent(event=name, job_id="crawl_01TEST", data={"total": 5})


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------


def test_signature_format() -> None:
    signature = sign(b'{"a":1}', "secret")
    assert signature.startswith("sha256=")
    assert len(signature) == len("sha256=") + 64


def test_signature_verifies() -> None:
    body = b'{"event":"completed"}'
    assert verify(body, "secret", sign(body, "secret"))


def test_signature_rejects_tampered_body() -> None:
    signature = sign(b'{"event":"completed"}', "secret")
    assert not verify(b'{"event":"failed"}', "secret", signature)


def test_signature_rejects_wrong_secret() -> None:
    body = b'{"a":1}'
    assert not verify(body, "other-secret", sign(body, "secret"))


def test_signature_is_over_raw_bytes_not_reserialised_json() -> None:
    """Re-serialising changes key order and whitespace, so a receiver hashing
    the bytes it received would disagree with us."""
    body = b'{"b":2,"a":1}'
    reserialised = json.dumps(json.loads(body)).encode()
    assert sign(body, "s") != sign(reserialised, "s")


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


@respx.mock
async def test_successful_delivery_sends_signature_header() -> None:
    route = respx.post("https://hooks.example.com/x").mock(return_value=httpx.Response(200))
    sender = WebhookSender()
    delivered = await sender.deliver("https://hooks.example.com/x", event(), secret="s")
    await sender.aclose()

    assert delivered
    request = route.calls.last.request
    assert verify(request.content, "s", request.headers["X-Signature-256"])


@respx.mock
async def test_payload_shape_matches_the_contract() -> None:
    route = respx.post("https://hooks.example.com/x").mock(return_value=httpx.Response(200))
    sender = WebhookSender()
    await sender.deliver("https://hooks.example.com/x", event("page"))
    await sender.aclose()

    payload = json.loads(route.calls.last.request.content)
    assert set(payload) == {"event", "jobId", "timestamp", "data"}
    assert payload["event"] == "page"
    assert payload["jobId"] == "crawl_01TEST"
    assert payload["timestamp"].endswith("Z")


@respx.mock
async def test_non_2xx_is_a_failed_delivery() -> None:
    respx.post("https://hooks.example.com/x").mock(return_value=httpx.Response(500))
    sender = WebhookSender()
    assert not await sender.deliver("https://hooks.example.com/x", event())
    await sender.aclose()


@respx.mock
async def test_connection_error_is_a_failed_delivery_not_an_exception() -> None:
    respx.post("https://hooks.example.com/x").mock(side_effect=httpx.ConnectError("no"))
    sender = WebhookSender()
    assert not await sender.deliver("https://hooks.example.com/x", event())
    await sender.aclose()


@respx.mock
async def test_retries_up_to_the_budget_then_dead_letters() -> None:
    route = respx.post("https://hooks.example.com/x").mock(return_value=httpx.Response(503))
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    sender = WebhookSender()
    delivered = await sender.deliver_with_retries(
        "https://hooks.example.com/x", event(), sleep=fake_sleep
    )
    await sender.aclose()

    assert not delivered
    assert route.call_count == MAX_ATTEMPTS
    # Exponential backoff, capped at five minutes.
    assert slept == [1, 5, 25, 60, 300]


@respx.mock
async def test_stops_retrying_once_delivered() -> None:
    responses = [httpx.Response(500), httpx.Response(200)]
    route = respx.post("https://hooks.example.com/x").mock(side_effect=responses)

    async def fake_sleep(seconds: float) -> None:
        return None

    sender = WebhookSender()
    delivered = await sender.deliver_with_retries(
        "https://hooks.example.com/x", event(), sleep=fake_sleep
    )
    await sender.aclose()

    assert delivered
    assert route.call_count == 2


# --------------------------------------------------------------------------
# Deduplication
# --------------------------------------------------------------------------


class FakeRedis:
    """Minimal stand-in for the SET NX / DELETE pair used by send_once."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int = 0) -> bool | None:
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0


@respx.mock
async def test_duplicate_event_is_sent_only_once() -> None:
    """A crash between the page write and the webhook must not send twice."""
    route = respx.post("https://hooks.example.com/x").mock(return_value=httpx.Response(200))
    redis: Any = FakeRedis()
    sender = WebhookSender()

    first = await send_once(sender, redis, "https://hooks.example.com/x", event())
    second = await send_once(sender, redis, "https://hooks.example.com/x", event())
    await sender.aclose()

    assert first and second
    assert route.call_count == 1, "the second delivery must be suppressed"


@respx.mock
async def test_failed_delivery_releases_the_dedup_key_for_a_later_retry() -> None:
    respx.post("https://hooks.example.com/x").mock(return_value=httpx.Response(500))
    redis: Any = FakeRedis()
    sender = WebhookSender()

    async def no_sleep(seconds: float) -> None:
        return None

    sender.deliver_with_retries = (  # type: ignore[method-assign]
        lambda url, evt, **kw: sender.deliver(url, evt)
    )
    delivered = await send_once(sender, redis, "https://hooks.example.com/x", event())
    await sender.aclose()

    assert not delivered
    assert redis.store == {}, "a failed delivery must not leave the event marked sent"


def test_dedup_key_distinguishes_pages() -> None:
    a = WebhookEvent(event="page", job_id="j1", page_id="page_1")
    b = WebhookEvent(event="page", job_id="j1", page_id="page_2")
    assert a.dedup_key() != b.dedup_key()


@pytest.mark.parametrize("name", ["started", "page", "completed", "failed"])
def test_every_contract_event_builds_a_payload(name: str) -> None:
    payload = WebhookEvent(event=name, job_id="j1").payload()
    assert payload["event"] == name
