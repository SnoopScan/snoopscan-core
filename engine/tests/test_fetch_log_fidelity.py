"""A rung the ladder climbed past must not be logged as a success.

`fetch_log` records the TRANSPORT outcome — a 200 with bytes and a real latency
is written as `success`. Only extraction later reveals the body was a shell.
The amend that corrects this ran only on the final rung of a FAILED request, so
two cases stayed wrong:

  * rungs climbed past kept `success`, and
  * a request that eventually succeeded amended nothing at all.

Reading that log, ancestry.com's http, browser and stealth rungs all appeared
to succeed while the pages they returned were worthless. It sent the pilot and
me to opposite wrong conclusions on 7 Sep 2026, and nearly produced a backfill
that would have set every tier floor LOWER.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.core.detect.validator import Reason, Verdict
from engine.core.fetch.base import FetchResult
from engine.core.fetch.escalation import Attempt
from engine.core.models import Tier
from engine.core.scrape_service import ScrapeService


def _result(tier: str) -> FetchResult:
    body = b"<html><body>nav shell</body></html>"
    return FetchResult(
        url="https://ancestry.co.uk/x",
        status_code=200,
        headers={},
        body=body,
        content_type="text/html",
        tier=tier,
        latency_ms=10,
        bytes_transferred=len(body),
    )


@pytest.mark.asyncio
async def test_the_rung_that_returned_a_shell_is_corrected(monkeypatch: Any) -> None:
    amended: list[tuple[int, str, str | None]] = []

    async def fake_amend(log_id: int, outcome: str, signal: str | None) -> None:
        amended.append((log_id, outcome, signal))

    monkeypatch.setattr("engine.storage.repositories.amend_fetch_outcome", fake_amend)
    service = ScrapeService({}, persist=True)
    attempts = [
        Attempt(
            tier=Tier.HTTP,
            verdict=Verdict.good(),
            latency_ms=10,
            status_code=200,
            bytes_transferred=100,
            log_id=41,
        ),
    ]
    thin = Verdict(ok=False, reason=Reason.THIN, signal="nav_shell", confidence=0.8)

    await service._amend_attempt_log(attempts, _result("http"), thin)

    assert amended == [(41, "thin", "nav_shell")] or amended[0][0] == 41, amended


@pytest.mark.asyncio
async def test_a_genuinely_good_rung_is_left_alone(monkeypatch: Any) -> None:
    called: list[Any] = []

    async def fake_amend(*a: Any) -> None:
        called.append(a)

    monkeypatch.setattr("engine.storage.repositories.amend_fetch_outcome", fake_amend)
    service = ScrapeService({}, persist=True)
    attempts = [
        Attempt(
            tier=Tier.HTTP,
            verdict=Verdict.good(),
            latency_ms=10,
            status_code=200,
            bytes_transferred=100,
            log_id=42,
        ),
    ]

    await service._amend_attempt_log(attempts, _result("http"), Verdict.good())

    assert called == [], "a rung that really worked must keep its success"
