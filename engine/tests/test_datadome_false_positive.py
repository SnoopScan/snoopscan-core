"""DataDome: a protected page is not a blocked page.

`x-datadome: protected` and a `datadome` cookie appear on every response from
a site DataDome protects — the allowed ones included. The header rule keyed on
them and recorded thebump.com's real article as a block 13 times over. What
distinguishes an actual challenge, measured on g2.com (7 Sep 2026): a 403,
`x-dd-b` / `x-datadome-cid` headers, and the `geo.captcha-delivery.com` loader
in a ~1.7KB body.
"""

from __future__ import annotations

import inspect

from engine.core.detect import validator
from engine.core.detect.validator import Reason, validate
from engine.core.fetch.base import FetchResult

# Varied prose, not a repeated line — a repeated line collapses.
ARTICLE = (
    "<html><head><title>Emma - Baby Name Meaning, Origin and Popularity</title>"
    '<script src="https://js.datadome.co/tags.js"></script></head><body>'
    "<h1>Emma</h1><p>Emma is a girl's name of Germanic origin meaning universal or whole.</p>"
    "<p>It has held a place near the top of the charts for two decades.</p>"
    "<p>Famous bearers include an Austen heroine and a run of actresses.</p>"
    "<p>Variants include Emmy, Ema and the older Irmina.</p>"
    "<p>The name pairs well with short middle names and long surnames alike.</p>"
    "<p>Parents often choose it for its warmth and its lack of nicknames.</p>"
    "</body></html>"
) * 40  # ~25KB, like a real page rather than an interstitial

CHALLENGE = (
    "<html><head><title>g2.com</title></head><body><script>"
    "var dd={'rt':'c','cid':'AHrlqAAAAAMArnXjLoZt44UAVh-oaw==',"
    "'host':'geo.captcha-delivery.com','cookie':'sWsY9h286Xn4o2FY'}"
    '</script><script src="https://ct.captcha-delivery.com/c.js"></script></body></html>'
)


def _r(status: int, body: str, headers: dict[str, str]) -> FetchResult:
    raw = body.encode()
    return FetchResult(
        url="https://www.example.test/x",
        status_code=status,
        headers=headers,
        body=raw,
        content_type="text/html",
        tier="impersonate",
        latency_ms=50,
        bytes_transferred=len(raw),
    )


PROTECTED = {"x-datadome": "protected", "set-cookie": "datadome=abc; Path=/; Secure, AKA_A2=A"}


def test_thebump_shaped_allowed_page_is_not_a_block() -> None:
    v = validate(_r(200, ARTICLE, PROTECTED))
    assert v.reason != Reason.BLOCKED, v
    assert v.signal != "datadome", v


def test_datadome_marketing_site_shape_is_not_a_block() -> None:
    # datadome.co itself was in the poisoned list. It just has the header.
    v = validate(_r(200, ARTICLE, {"x-datadome": "protected"}))
    assert v.reason != Reason.BLOCKED


def test_g2_shaped_challenge_is_a_block() -> None:
    v = validate(_r(403, CHALLENGE, {**PROTECTED, "x-dd-b": "2", "x-datadome-cid": "AHrlqAAA=="}))
    assert (v.ok, v.reason, v.signal, v.vendor) == (False, Reason.BLOCKED, "datadome", "datadome")


def test_a_challenge_served_as_200_is_still_a_block() -> None:
    # DataDome will serve the interstitial with a 200 to some clients. The
    # loader in the body is the tell, not the status.
    v = validate(_r(200, CHALLENGE, PROTECTED))
    assert (v.reason, v.signal) == (Reason.BLOCKED, "datadome")


def test_x_dd_b_on_a_real_page_is_not_a_challenge() -> None:
    # etsy.com, measured: x-dd-b=259 on a 403 carrying the full 600KB results
    # page. The header flags the client; the page still came. The first
    # version of this test asserted the opposite and was wrong.
    v = validate(_r(200, ARTICLE, {**PROTECTED, "x-dd-b": "259"}))
    assert v.reason != Reason.BLOCKED


def test_a_403_that_carries_the_full_page_is_the_page() -> None:
    v = validate(_r(403, ARTICLE, {**PROTECTED, "x-dd-b": "259"}))
    assert v.reason != Reason.BLOCKED, "a 600KB 403 is content, not a refusal"


def test_a_403_with_a_tiny_body_is_a_refusal() -> None:
    v = validate(_r(403, "<html><body>Access Denied</body></html>", PROTECTED))
    assert (v.reason, v.signal) == (Reason.BLOCKED, "datadome")


def test_the_bare_header_rule_is_gone() -> None:
    src = inspect.getsource(validator._layer1)
    # The old rule returned BLOCKED on the header alone; the new one must gate
    # on a challenge marker before it can.
    i = src.index("x-datadome")
    following = src[i : i + 900]
    assert "challenged" in following and "captcha-delivery.com" in following


# --------------------------------------------------------------------------
# Content verdicts must not tax the domain
# --------------------------------------------------------------------------
#
# apply_block raises a domain's tier floor for a week. A verdict about the
# PAGE — it had no words, it was all nav — must never do that: 217 domains
# were sitting on a raised floor with zero successes (7 Sep 2026). A verdict
# about the TARGET's behaviour still should.


def test_content_verdicts_are_thin_and_block_verdicts_are_not() -> None:
    import inspect

    from engine.core.detect import validator

    src = inspect.getsource(validator)
    content_signals = ("near_empty", "link_only", "nav_shell", "empty_content")
    block_signals = ("challenge_title", "below_baseline", "implausible_content")

    for signal in content_signals:
        i = src.index(f'signal="{signal}"')
        window = src[max(0, i - 400) : i]
        assert "Reason.THIN" in window, f"{signal} still taxes the domain as a block"

    for signal in block_signals:
        i = src.index(f'signal="{signal}"')
        window = src[max(0, i - 400) : i]
        assert "Reason.SOFT_BLOCK" in window or "Reason.BLOCKED" in window, (
            f"{signal} stopped being treated as a block"
        )


def test_a_javascript_shell_climbs_without_marking_the_domain() -> None:
    from engine.core.detect.validator import ExtractionSummary, Reason, validate
    from engine.core.fetch.base import FetchResult

    # 250KB of markup, a footer's worth of words: tidal.com/browse, measured.
    body = (
        "<html><body><div id='root'></div>" + "<span></span>" * 20_000 + "</body></html>"
    ).encode()
    result = FetchResult(
        url="https://example.test/browse",
        status_code=200,
        headers={},
        body=body,
        content_type="text/html",
        tier="stealth_hard",
        latency_ms=900,
        bytes_transferred=len(body),
    )
    verdict = validate(
        result,
        extraction=ExtractionSummary(
            markdown="Get Started\n\nDiscover\n\nAccount\n\nCompany",
            word_count=6,
            char_count=40,
            confidence=0.9,
            title="A streaming app",
            link_count=2,
        ),
    )
    assert verdict.reason == Reason.THIN, verdict
    assert verdict.reason != Reason.SOFT_BLOCK, "a shell must not raise the domain's floor"
