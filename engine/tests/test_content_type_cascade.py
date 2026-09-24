"""Regression tests for a three-stage cascade found on a live crawl.

A WordPress sitemap lists every uploaded image alongside the posts. Those
image URLs entered the frontier as pages; a JPEG fetched fine but extracted to
zero words; the validator called that a soft block; the soft block raised the
domain's working tier twice, to `browser`; and because the browser tier is not
deployed yet, the escalation ladder came back EMPTY and every subsequent page
failed with zero fetch attempts.

The engine took itself offline for that domain and the failures looked exactly
like the target blocking us.
"""

from __future__ import annotations

import pytest

from engine.core.detect.validator import (
    DomainStats,
    ExtractionSummary,
    Reason,
    is_extractable,
    validate,
)
from engine.core.fetch.base import FetchResult
from engine.core.fetch.escalation import (
    DomainProfile,
    EscalationController,
    ladder_from,
)
from engine.core.models import Tier
from engine.core.urls import has_skipped_extension
from engine.tests.test_escalation import ScriptedFetcher, request


def result(content_type: str, body: bytes = b"x" * 50_000) -> FetchResult:
    return FetchResult(
        url="https://example.com/asset",
        status_code=200,
        headers={},
        body=body,
        content_type=content_type,
        tier="http",
        latency_ms=50,
        bytes_transferred=len(body),
    )


# --------------------------------------------------------------------------
# Stage 1 — image URLs must never enter the frontier as pages
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/wp-content/uploads/2025/09/Photo.jpg",
        "https://example.com/a.jpeg",
        "https://example.com/a.PNG",
        "https://example.com/logo.svg",
        "https://example.com/hero.webp",
        "https://example.com/icon.ico",
        "https://example.com/shot.avif",
    ],
)
def test_image_urls_are_skipped(url: str) -> None:
    assert has_skipped_extension(url)


def test_pdf_is_still_crawlable() -> None:
    """PDF is a supported parser target, so it must NOT be swept up with the
    images."""
    assert not has_skipped_extension("https://example.com/report.pdf")


def test_ordinary_pages_are_unaffected() -> None:
    assert not has_skipped_extension("https://example.com/keto-diet-meal-prepping/")


# --------------------------------------------------------------------------
# Stage 2 — unsupported content is a target error, never a block
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content_type",
    ["image/jpeg", "image/png", "video/mp4", "application/zip", "font/woff2"],
)
def test_binary_content_is_a_target_error_not_a_block(content_type: str) -> None:
    """Classifying it as a block escalates to a browser tier to re-fetch an
    image, and raises the domain's working tier on the way."""
    verdict = validate(result(content_type))
    assert verdict.reason == Reason.TARGET_ERROR
    assert verdict.signal == "unsupported_content_type"


def test_binary_content_stays_a_target_error_after_extraction() -> None:
    """The post-extraction pass is where the soft block used to fire: zero
    words against a 50KB body looked exactly like a gated page."""
    empty = ExtractionSummary(word_count=0, char_count=0, confidence=0.0)
    verdict = validate(result("image/jpeg"), DomainStats(), empty)
    assert verdict.reason == Reason.TARGET_ERROR
    assert verdict.reason != Reason.SOFT_BLOCK


@pytest.mark.parametrize(
    "content_type",
    [
        "text/html; charset=utf-8",
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "application/json",
        "application/pdf",
        None,
    ],
)
def test_extractable_types_are_allowed_through(content_type: str | None) -> None:
    assert is_extractable(content_type)


def test_missing_content_type_is_treated_as_extractable() -> None:
    """Plenty of servers omit it; refusing those would lose real pages."""
    assert is_extractable(None)
    assert is_extractable("")


def test_a_real_html_page_still_soft_blocks_when_empty() -> None:
    """The content-type gate must not disable soft-block detection for the
    case it exists to catch."""
    empty = ExtractionSummary(word_count=2, char_count=10, confidence=0.05)
    verdict = validate(result("text/html", b"<html>" + b"y" * 40_000), DomainStats(), empty)
    # The gate must not disable detection: the page is still refused. It is THIN
    # rather than SOFT_BLOCK because nothing here says a WAF was involved.
    assert not verdict.ok
    assert verdict.reason == Reason.THIN


# --------------------------------------------------------------------------
# Stage 3 — an unavailable tier must degrade, not brick the domain
# --------------------------------------------------------------------------


def test_ladder_falls_back_when_the_start_tier_is_not_deployed() -> None:
    """A profile raised to `browser` while only HTTP tiers exist must still
    produce something to try."""
    ladder = ladder_from(Tier.BROWSER, available={Tier.HTTP, Tier.IMPERSONATE})
    assert ladder == [Tier.IMPERSONATE], "should fall back to the best tier we have"


def test_ladder_prefers_available_tiers_at_or_above_the_start() -> None:
    ladder = ladder_from(Tier.IMPERSONATE, available={Tier.HTTP, Tier.IMPERSONATE})
    assert ladder == [Tier.IMPERSONATE]


def test_ladder_is_empty_only_when_nothing_is_deployed() -> None:
    assert ladder_from(Tier.BROWSER, available=set()) == []


async def test_a_raised_profile_does_not_stop_the_engine_fetching() -> None:
    """The regression itself: 20 pages failed with `no_tier_available` and
    zero fetch attempts, which looked identical to the target blocking us."""
    tier0 = ScriptedFetcher(name="http")
    tier1 = ScriptedFetcher(name="impersonate")
    controller = EscalationController({Tier.HTTP: tier0, Tier.IMPERSONATE: tier1})

    # A profile poisoned up to a tier that is not built yet.
    profile = DomainProfile("example.com", min_working_tier=Tier.BROWSER)
    outcome = await controller.fetch(request(), profile)

    assert tier1.calls == 1, "the best available tier must still be attempted"
    assert outcome.succeeded
    assert outcome.verdict.signal != "no_tier_available"
