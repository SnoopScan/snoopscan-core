"""A 502/503/504 is retried once at the same tier; nothing above tier 0 fixes an
origin error, so it never escalates (6 Sep 2026)."""

from __future__ import annotations

from typing import Any

from engine.core.fetch import escalation as esc
from engine.core.fetch.base import FetchResult
from engine.core.fetch.escalation import DomainProfile, EscalationController
from engine.core.models import Tier

GOOD = (
    "<html><body><nav><a href='/a'>a</a></nav><main><h1>Up</h1>"
    + "<p>The origin answered on the second try, with real content.</p>" * 20
    + "</main></body></html>"
).encode()


class _Flaky:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses, self.calls = statuses, 0

    async def fetch(self, req: Any) -> FetchResult:
        status = self.statuses[min(self.calls, len(self.statuses) - 1)]
        self.calls += 1
        body = GOOD if status == 200 else b"<html><body>Bad gateway</body></html>"
        return FetchResult(
            url=req.url,
            status_code=status,
            headers={"content-type": "text/html"},
            body=body,
            content_type="text/html",
            tier="http",
            latency_ms=5,
            bytes_transferred=len(body),
        )


async def test_a_single_503_is_retried_once_and_succeeds(monkeypatch: Any) -> None:
    monkeypatch.setattr(esc, "TRANSIENT_RETRY_PAUSE_MS", 1)
    http = _Flaky([503, 200])
    browser = _Flaky([200])
    ctl = EscalationController({Tier.HTTP: http, Tier.BROWSER: browser})
    from engine.core.fetch.base import FetchRequest

    req = FetchRequest(url="https://x.test/", timeout_ms=30_000)
    out = await ctl.fetch(req, DomainProfile("x.test"))
    assert out.succeeded and out.result is not None
    assert http.calls == 2, "one retry, same tier"
    assert browser.calls == 0, "an origin error never climbs"


async def test_a_persistent_503_stops_after_one_retry(monkeypatch: Any) -> None:
    monkeypatch.setattr(esc, "TRANSIENT_RETRY_PAUSE_MS", 1)
    http = _Flaky([503, 503, 503])
    ctl = EscalationController({Tier.HTTP: http, Tier.BROWSER: _Flaky([200])})
    from engine.core.fetch.base import FetchRequest

    req = FetchRequest(url="https://x.test/", timeout_ms=30_000)
    out = await ctl.fetch(req, DomainProfile("x.test"))
    assert not out.succeeded
    assert http.calls == 2, "retry once, then report the origin error"
    assert out.verdict.signal == "status_503"


async def test_a_404_is_never_retried(monkeypatch: Any) -> None:
    monkeypatch.setattr(esc, "TRANSIENT_RETRY_PAUSE_MS", 1)
    http = _Flaky([404, 200])
    ctl = EscalationController({Tier.HTTP: http})
    from engine.core.fetch.base import FetchRequest

    req = FetchRequest(url="https://x.test/", timeout_ms=30_000)
    out = await ctl.fetch(req, DomainProfile("x.test"))
    assert http.calls == 1 and not out.succeeded
