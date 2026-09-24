"""A 403 block page rendered by a browser tier is still a block page.

indeed.com, 21 Sep 2026: the browser tier fetched Indeed's own "Request
Blocked" page — 403, 59 words, a Cloudflare Ray ID — and it was returned as a
success. Rendering had grown it to ~44KB, past the size at which a 403 is
handed on as "might be a real page" (etsy.com serves whole results pages with
a 403). Nothing later caught it: the wording is not Cloudflare's stock "Sorry,
you have been blocked", the title was not a known challenge title, and 59
words is over the near-empty line. So the ladder stopped, the retry from
another country never ran — the one that returned the real listings — and
the customer was charged for a block page.
"""

from __future__ import annotations

import pytest

from engine.core.detect import validator
from engine.core.detect.validator import ExtractionSummary, Reason, validate
from engine.core.fetch.base import FetchResult

# Indeed's page as the browser tier stored it: a short message wrapped in a
# rendered document heavy with inline script and style.
_PADDING = "<script>" + ("window.__cfg=" + '{"k":"' + "x" * 200 + '"};') * 150 + "</script>"
INDEED_BLOCK = (
    "<html><head><title>Blocked - Indeed.com</title><style>"
    + "body{margin:0}" * 300
    + "</style></head><body>"
    + _PADDING
    + "<h1>Request Blocked</h1>"
    "<p>You have been blocked. If you believe this in error, please go to support.indeed.com and "
    "reference the following information: Your Ray ID for this request is a3ec2e326d37def3 "
    "Your current IP for this request is 192.0.2.10</p>"
    '<a href="https://www.indeed.com/">Return home</a>'
    '<p>Need more help?</p><a href="https://www.indeed.com/support/contact">Contact us</a>'
    "</body></html>"
)

# Etsy's shape: a whole results page served behind a 403. It must still pass.
ETSY_403 = (
    "<html><head><title>Handmade ceramic mugs - Etsy</title></head><body>"
    + "".join(
        f"<article><h2>Stoneware mug number {i}</h2><p>Wheel thrown in small batches, glazed in "
        f"speckled oatmeal, holds about {300 + i} millilitres and ships wrapped in recycled paper "
        f"from a studio that has sold {1000 + i * 7} of them.</p></article>"
        for i in range(120)
    )
    + "</body></html>"
)


def _r(status: int, body: str, tier: str = "browser") -> FetchResult:
    raw = body.encode()
    return FetchResult(
        url="https://www.example.test/jobs",
        status_code=status,
        headers={},
        body=raw,
        content_type="text/html",
        tier=tier,
        latency_ms=900,
        bytes_transferred=len(raw),
    )


def test_the_rendered_block_page_is_big_enough_to_have_slipped_past_the_size_rule() -> None:
    # The premise: this is over the 20KB line, which is how it escaped.
    assert len(INDEED_BLOCK.encode()) > 20_000


def _extracted(words: int, title: str) -> ExtractionSummary:
    # What the pipeline passes on its second, post-extraction look. The title
    # is deliberately NOT a known challenge title, and the signature list is
    # private (absent from the open core), so nothing but the 403 rule itself
    # can catch the page: the test proves that rule, in both trees.
    return ExtractionSummary(
        word_count=words, char_count=words * 6, confidence=0.6, link_count=2, title=title
    )


def test_a_rendered_403_block_page_is_a_block_not_a_success() -> None:
    v = validate(_r(403, INDEED_BLOCK), extraction=_extracted(59, "Indeed"))
    assert not v.ok, v
    assert v.reason == Reason.BLOCKED, v


def test_the_403_rule_catches_it_with_no_signatures_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    # The open core ships without the signature list; this is that tree.
    # Without the 403 rule the page passes, exactly as it did live.
    monkeypatch.setattr(validator, "_load_signatures", lambda: ((), frozenset(), ()))
    v = validate(_r(403, INDEED_BLOCK), extraction=_extracted(59, "Indeed"))
    assert v.reason == Reason.BLOCKED and v.signal == "status_403", v


def test_a_403_that_carries_a_whole_page_is_still_the_page() -> None:
    v = validate(_r(403, ETSY_403, tier="impersonate"), extraction=_extracted(3000, "Mugs - Etsy"))
    assert v.ok, v


def test_the_same_block_wording_in_a_200_article_is_not_a_block() -> None:
    article = ETSY_403.replace(
        "<body>",
        "<body><p>If you have been blocked by a site, the page often shows "
        "a Ray ID you can quote to support.</p>",
    )
    assert validate(_r(200, article, tier="impersonate"), extraction=_extracted(3000, "Mugs")).ok
