"""A page whose content is an embed is content, not an empty block.

Reported 12 Sep 2026. `horrorgames.io/sorry-bob.embed` is a 6 KB 200 whose body
is one <iframe> pointing at the game, plus a thumbnail — no challenge, no
Cloudflare interstitial. The engine:

  1. read it as `empty_content` (the iframe is cross-origin, so nothing
     extracts to prose),
  2. a stray low-confidence signature corroborated that to SOFT_BLOCK,
  3. which escalated through http -> browser -> stealth -> stealth_hard,
  4. and returned BLOCKED — four fetches and deep-tier credits for a page the
     cheapest tier fetched clean.

A game-in-an-iframe, a video, a canvas app: the embed IS the page. It is at
worst thin, never a block, and it must not escalate.
"""

from __future__ import annotations

import pytest

from engine.core.detect.validator import ExtractionSummary, Reason, validate
from engine.core.fetch.base import FetchResult

EMBED_PAGE = (
    b"<!doctype html><html><head><title>Sorry Bob</title></head><body>"
    b"<nav><a href='/'>Home</a> <a href='/new'>New</a></nav>"
    b"<iframe id='iframehtml5' src='https://gamea.azgame.io/sorry-bob/'"
    b" width='100%' height='600'></iframe>"
    b"<img src='/thumbs/sorry-bob.png' alt='Sorry Bob'>"
    b"</body></html>"
)

# A real Cloudflare Turnstile challenge, also delivered in an iframe.
CHALLENGE_PAGE = (
    b"<!doctype html><html><head><title>Just a moment...</title></head><body>"
    b"<div class='cf-challenge'></div>"
    b"<iframe src='https://challenges.cloudflare.com/turnstile/v0/'"
    b" style='display:none'></iframe>"
    b"</body></html>"
)


def _result(body: bytes, status: int = 200) -> FetchResult:
    return FetchResult(
        url="https://horrorgames.io/sorry-bob.embed",
        status_code=status,
        headers={"content-type": "text/html"},
        body=body,
        content_type="text/html",
        tier="http",
        latency_ms=10,
        bytes_transferred=len(body),
    )


def _empty_extraction() -> ExtractionSummary:
    """What the iframe page extracts to: no prose at all."""
    return ExtractionSummary(
        markdown="",
        page_type="unknown",
        external_link_count=0,
        has_images=False,
        has_author=False,
        word_count=0,
        char_count=0,
        confidence=0.65,
        link_count=2,
        title="Sorry Bob",
        extraction_path="heuristic",
    )


def test_an_iframe_page_is_valid_content_not_a_block() -> None:
    verdict = validate(_result(EMBED_PAGE), None, _empty_extraction())

    assert verdict.ok, f"an embed page must be OK, got {verdict.reason}/{verdict.signal}"
    assert verdict.reason is None


@pytest.mark.parametrize("tag", [b"iframe", b"embed", b"object", b"video", b"canvas"])
def test_every_embed_element_counts_as_content(tag: bytes) -> None:
    body = (
        b"<html><body><" + tag + b" src='https://cdn.example.com/x'></" + tag + b"></body></html>"
    )
    assert validate(_result(body), None, _empty_extraction()).ok


def test_a_challenge_iframe_is_still_a_block() -> None:
    """The exclusion that keeps this safe: a turnstile is also an iframe, and it
    must not be waved through as content."""
    verdict = validate(_result(CHALLENGE_PAGE), None, _empty_extraction())

    assert not verdict.ok, "a Cloudflare challenge iframe must not read as content"


def test_a_genuinely_empty_page_is_still_thin() -> None:
    """No embed, no prose: still nothing. The gate must not bless every empty
    body — only ones built around an embed."""
    verdict = validate(_result(b"<html><body></body></html>"), None, _empty_extraction())

    assert not verdict.ok
    assert verdict.reason is Reason.THIN


async def test_the_ladder_does_not_climb_for_an_embed_page() -> None:
    """The cost half of the bug: a clean 2xx embed page must be answered at the
    tier that fetched it, not escalated through the browser and stealth rungs."""
    from engine.core.fetch.base import FetchRequest
    from engine.core.fetch.escalation import DomainProfile, EscalationController
    from engine.core.models import Tier

    calls: list[str] = []

    class _Rung:
        def __init__(self, name: str) -> None:
            self.name = name

        async def fetch(self, req: FetchRequest) -> FetchResult:
            calls.append(self.name)
            return _result(EMBED_PAGE)

        async def healthcheck(self) -> bool:
            return True

    # The controller validates transport only (no extraction), so an embed page
    # with real bytes passes layer 1 and never reaches the thin gate here — but
    # the point stands: it must answer at http and not climb.
    controller = EscalationController(
        {Tier.HTTP: _Rung("http"), Tier.BROWSER: _Rung("browser"), Tier.STEALTH: _Rung("stealth")}
    )
    outcome = await controller.fetch(
        FetchRequest(url="https://horrorgames.io/x.embed"), DomainProfile("horrorgames.io")
    )

    assert calls == ["http"], f"climbed past a clean embed page: {calls}"
    assert outcome.succeeded


@pytest.mark.parametrize(
    "host",
    [
        b"js.hcaptcha.com/1/api.js",
        b"www.google.com/recaptcha/api.js",
        b"challenges.cloudflare.com/turnstile",
        b"client-api.arkoselabs.com",
    ],
)
def test_a_captcha_iframe_from_any_vendor_is_a_block(host: bytes) -> None:
    """The exclusion covers vendors the signature host-list does not: hcaptcha,
    recaptcha, turnstile and arkoselabs are only caught by `_CAPTCHA_HOSTS`.
    Remove that list and this is the test that goes red."""
    body = b"<html><body><iframe src='https://" + host + b"'></iframe></body></html>"
    assert not validate(_result(body), None, _empty_extraction()).ok, f"{host!r} waved through"


# ------------------------------------------------------------------------
# An embed EXCUSES an empty extraction only when it IS the page.
#
# A retail home page reached the caller as a success with 0 words (Sep 2026).
# Its extraction had failed, and the embed exemption waved it through because
# the page carried iframes — tracking-pixel sandboxes and a cart-sync frame,
# every one of them aria-hidden="true", and an SMS sign-up pop-up. None was the
# page; the page was 600 KB with 97,000 characters of its own text.


def _pixels() -> bytes:
    return b"".join(
        b'<iframe tabindex="-1" aria-hidden="true" name="web-pixel-sandbox-%d"'
        b' src="https://shop.example.com/web-pixels/%d/sandbox"></iframe>' % (i, i)
        for i in range(5)
    )


def test_hidden_tracking_iframes_are_not_content() -> None:
    body = b"<html><body><h1>Shop</h1>" + _pixels() + b"</body></html>"
    verdict = validate(_result(body), None, _empty_extraction())
    assert not verdict.ok, "iframes that declare themselves hidden are not the page"


def test_a_page_full_of_its_own_text_is_not_an_embed_page() -> None:
    """Even a visible iframe cannot excuse losing a page that has real text."""
    prose = b"".join(
        b"<p>Paragraph %d of the page's own copy, which the extraction lost.</p>" % i
        for i in range(400)
    )
    # Furnished like the real page — navigation, search, a site's worth of
    # links — so the near-empty check stands aside exactly as it did there and
    # the embed exemption is what gets tested.
    nav = (
        b'<header><nav><form><input type="search" name="q"></form>'
        + b"".join(b'<a href="/c/%d">Category %d</a>' % (i, i) for i in range(30))
        + b"</nav></header>"
    )
    body = (
        b"<html><body>"
        + nav
        + b"<main>"
        + prose
        + b"</main>"
        + _pixels()
        + b'<iframe title="Sign up for offers" src="https://popups.example.net/offer"'
        + b' style="width:100%"></iframe></body></html>'
    )
    verdict = validate(_result(body), None, _empty_extraction())
    assert not verdict.ok
    assert verdict.reason is Reason.THIN
