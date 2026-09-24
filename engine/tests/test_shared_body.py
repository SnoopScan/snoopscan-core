"""The same bytes for different URLs is the site's chrome, not the page.

behindthename.com returned an identical 3,836-character menu bar for
/name/aspen and /name/brandon (sha256 a88a91d43ebb, 7 Sep 2026); ancestry did
the same before it. No per-page heuristic can catch this — 3.8 KB of nav
clears every length threshold and one such response is indistinguishable from
a genuinely short page. It is only visible across URLs.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.core.detect.validator import Reason, Verdict
from engine.core.scrape_service import SHARED_BODY_MIN_URLS, ScrapeService

MENU_BAR = "Behind the Name Names Themes Random Submit Sign In " * 40


def _service() -> ScrapeService:
    service = ScrapeService({}, persist=True)
    return service


@pytest.mark.asyncio
async def test_a_body_seen_on_enough_other_urls_is_rejected(monkeypatch: Any) -> None:
    async def fake_record(domain: str, ch: bytes, uh: bytes) -> int:
        return SHARED_BODY_MIN_URLS

    monkeypatch.setattr("engine.storage.repositories.record_content_fingerprint", fake_record)
    verdict = await _service()._shared_body_verdict(
        "behindthename.com",
        "https://www.behindthename.com/name/aspen",
        MENU_BAR,
        Verdict.good(),
    )
    assert not verdict.ok
    assert verdict.signal == "shared_body"
    assert verdict.details["shared_by_urls"] == SHARED_BODY_MIN_URLS


@pytest.mark.asyncio
async def test_it_is_THIN_so_it_never_raises_the_domains_floor(monkeypatch: Any) -> None:
    """A verdict about a PAGE must not tax every other URL on the host.
    SOFT_BLOCK would call apply_block and raise the tier floor for a week."""

    async def fake_record(domain: str, ch: bytes, uh: bytes) -> int:
        return SHARED_BODY_MIN_URLS + 5

    monkeypatch.setattr("engine.storage.repositories.record_content_fingerprint", fake_record)
    verdict = await _service()._shared_body_verdict(
        "behindthename.com",
        "https://www.behindthename.com/name/sage",
        MENU_BAR,
        Verdict.good(),
    )
    assert verdict.reason == Reason.THIN
    assert verdict.reason != Reason.SOFT_BLOCK


@pytest.mark.asyncio
async def test_two_urls_is_not_enough(monkeypatch: Any) -> None:
    """Two identical bodies is a coincidence a real site produces."""

    async def fake_record(domain: str, ch: bytes, uh: bytes) -> int:
        return SHARED_BODY_MIN_URLS - 1

    monkeypatch.setattr("engine.storage.repositories.record_content_fingerprint", fake_record)
    verdict = await _service()._shared_body_verdict(
        "example.com", "https://example.com/a", MENU_BAR, Verdict.good()
    )
    assert verdict.ok


@pytest.mark.asyncio
async def test_a_broken_fingerprint_store_never_fails_a_scrape(monkeypatch: Any) -> None:
    async def boom(domain: str, ch: bytes, uh: bytes) -> int:
        raise RuntimeError("table is gone")

    monkeypatch.setattr("engine.storage.repositories.record_content_fingerprint", boom)
    verdict = await _service()._shared_body_verdict(
        "example.com", "https://example.com/a", MENU_BAR, Verdict.good()
    )
    assert verdict.ok, "a diagnostic must never take down the request"


@pytest.mark.asyncio
async def test_an_empty_body_is_left_to_the_thin_detector(monkeypatch: Any) -> None:
    called = False

    async def fake_record(domain: str, ch: bytes, uh: bytes) -> int:
        nonlocal called
        called = True
        return 99

    monkeypatch.setattr("engine.storage.repositories.record_content_fingerprint", fake_record)
    verdict = await _service()._shared_body_verdict(
        "example.com", "https://example.com/a", "", Verdict.good()
    )
    assert verdict.ok and not called


# --------------------------------------------------------------------------
# The false positive this rule had: a site that legitimately answers every
# query variant with the same canonical page.
# --------------------------------------------------------------------------

REAL_PAGE = (
    "# Example Domain\n\n"
    "This domain is for use in documentation examples without needing "
    "permission. Avoid use in operations.\n\n"
    "[Learn more](https://iana.org/domains/example)"
)


@pytest.mark.asyncio
async def test_a_real_page_shared_across_query_variants_is_not_a_decoy(
    monkeypatch: Any,
) -> None:
    """example.com answered four `?probe=` variants with its real page and
    was failed as a decoy across all four tiers. Most sites ignore tracking
    parameters, so this shape is common, not exotic.
    """

    async def _seen(domain: str, ch: bytes, uh: bytes) -> int:
        return SHARED_BODY_MIN_URLS + 3

    monkeypatch.setattr("engine.storage.repositories.record_content_fingerprint", _seen)

    verdict = await _service()._shared_body_verdict(
        "example.com", "https://example.com/?probe=4", REAL_PAGE, Verdict.good()
    )

    assert verdict.ok, "a canonical page served at many URLs is not chrome"


@pytest.mark.asyncio
async def test_a_menu_is_still_a_decoy_however_it_is_laid_out(
    monkeypatch: Any,
) -> None:
    """Both shapes a menu arrives in: one long line, and one item per line.
    The nav-shell test alone missed the first, because that is a single line
    and a shell needs eight."""

    async def _seen(domain: str, ch: bytes, uh: bytes) -> int:
        return SHARED_BODY_MIN_URLS

    monkeypatch.setattr("engine.storage.repositories.record_content_fingerprint", _seen)

    per_line = "\n".join(["Home", "Names", "Browse", "Random", "Sign in", "About"] * 20)
    for body in (MENU_BAR, per_line):
        verdict = await _service()._shared_body_verdict(
            "behindthename.com", "https://behindthename.com/name/x", body, Verdict.good()
        )
        assert not verdict.ok, body[:40]
        assert verdict.signal == "shared_body"
