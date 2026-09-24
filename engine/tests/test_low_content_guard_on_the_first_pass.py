"""Before extraction, "no content" must be read from the PAGE, not from the
absence of an extraction that has not run yet.

Signatures that only mean something on an empty page carry
`requires_low_content` — a Turnstile widget, a consent banner, a login
sentence, a reCAPTCHA. The guard asked the extraction how many words it found,
and on the pre-extraction pass there IS no extraction, so it read "absent".
Any page carrying one of those markers was refused before anything looked at
it, provided the furniture scan missed too — and that scan reads only the
first 200,000 characters, which on a big page is all script.

Measured on a live hosting company's home page (Sep 2026): 924 KB, 1,429 words
of real content, fetched at HTTP 200 by every rung, and refused as a Cloudflare
block. A plain curl of the same URL returned it in full.
"""

from __future__ import annotations

import pytest

from engine.core.detect import validator
from engine.core.detect.validator import Reason, validate
from engine.core.fetch.base import FetchResult

WIDGET = b'<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>'


def _result(body: bytes) -> FetchResult:
    return FetchResult(
        url="https://example.com/",
        status_code=200,
        headers={"content-type": "text/html"},
        body=body,
        content_type="text/html",
        tier="browser",
        latency_ms=40,
        bytes_transferred=len(body),
    )


def _big_real_page() -> bytes:
    """A real page shaped like the one that broke: the widget in the head, a
    quarter of a megabyte of inline script, and the readable page after it."""
    # Visible text, not script: the scan window is measured after scripts are
    # stripped, so only real text pushes the furniture out of view.
    filler = b"".join(
        b"<p>Notice %d: this page opens with a long block of small print.</p>" % i
        for i in range(4_000)
    )
    prose = b"".join(
        b"<p>Paragraph %d of the page, describing a plan, its price and what it includes.</p>" % i
        for i in range(120)
    )
    nav = b"".join(b'<a href="/p/%d">Page %d</a>' % (i, i) for i in range(60))
    return (
        b"<html><head><title>Bring your idea online</title>"
        + WIDGET
        + b"</head><body>"
        + filler
        + b'<header><nav><form><input type="search" name="q"></form>'
        + nav
        + b"</nav></header><main>"
        + prose
        + b"</main>"
        # And a heavy block after it, as a modern page has: the navigation ends
        # up in neither the head nor the tail of the scan window.
        + b"<script>window.__DATA__ = '"
        + (b"d" * 300_000)
        + b"';</script>"
        + b"</body></html>"
    )


def test_a_big_real_page_is_not_blocked_before_extraction_runs() -> None:
    verdict = validate(_result(_big_real_page()))  # pre-extraction: no extraction yet
    assert verdict.ok, f"refused before reading it: {verdict.signal}"


def test_a_challenge_page_is_still_caught_on_that_same_pass() -> None:
    """The guard must still do its job: a page with the widget and nothing
    else IS the interstitial, and catching it early saves every later rung."""
    # Needs the WAF signature list, which the open core does not ship: there
    # the page is correctly unrecognised, so this assertion cannot hold.
    if validator._signature_path() is None:
        pytest.skip("needs the block signature list (engine/knowledge)")
    body = b"<html><head><title>Just a moment...</title>" + WIDGET + b"</head><body></body></html>"
    verdict = validate(_result(body))
    assert not verdict.ok
    assert verdict.reason in (Reason.BLOCKED, Reason.SOFT_BLOCK)
