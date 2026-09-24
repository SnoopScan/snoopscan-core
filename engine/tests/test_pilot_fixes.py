"""The six issues from the first pilot integration (7 Sep 2026), pinned.

Issue 1 turned out not to be ours — the reporter's shell `echo` unescaped the
response — but the envelope is now asserted strict-JSON so that stays true.
Issues 2–5 were real and share one root: a fetch that answered was treated as
a page that answered.
"""

from __future__ import annotations

import inspect
import json

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from engine.core.detect.validator import (
    ExtractionSummary,
    Reason,
    is_nav_shell,
    validate,
)
from engine.core.fetch.base import FetchResult
from engine.core.fetch.escalation import TRANSIENT_STATUSES, starting_tier
from engine.core.models import ScrapeOptions, Tier

# ---------------------------------------------------------------- fixtures --

# The ancestry.com body, shape-for-shape: fifty short menu labels, no sentence
# anywhere, ~96 words. Varied on purpose — a repeated line collapses.
SHELL = "\n".join(
    [
        "Skip Ancestry main menu",
        "Skip to Footer",
        "Home",
        "Trees",
        "Search",
        "Memories",
        "DNA",
        "Subscribe",
        "Cart",
        "Help",
        "Sign In",
        "Home",
        "Trees",
        "Loading",
        "Trees you own",
        "Shared with you",
        "Tree tools",
        "My tree",
        "Import a tree",
        "Search",
        "All collections",
        "Census & voter lists",
        "Birth, marriage & death",
        "Immigration & travel",
        "Military",
        "Card catalog",
        "Member directory",
        "Memories",
        "Photos",
        "Stories",
        "Audio",
        "Upload",
        "DNA",
        "Your results",
        "Matches",
        "Origins",
        "Traits",
        "Activate a kit",
        "Buy a kit",
        "Subscribe",
        "Membership plans",
        "Gift memberships",
        "Redeem a gift",
        "Help",
        "Support center",
        "Contact us",
        "Learning hub",
        "Community",
        "Sign out",
    ]
)
SHELL_WORDS = len(SHELL.split())

EXAMPLE_COM = (
    "Example Domain\n"
    "This domain is for use in illustrative examples in documents.\n"
    "You may use this domain in literature without prior coordination or asking for permission."
)
ARTICLE = "\n".join(
    [
        "Why the survey needed so many screens",
        "The instrument captures a field of view two hundred times wider than its predecessor.",
        "Each exposure is stitched from eighteen detectors, and a single night "
        "fills several terabytes.",
        "Displaying one frame at native resolution would take a wall of four-kilo panels.",
        "The team instead built a tiled viewer that streams only the region under the cursor.",
        "Early results already show structure in the galactic halo that no catalogue predicted.",
        "A second data release is scheduled for the autumn, once calibration settles.",
        "Observers can request time through the standard proposal cycle.",
    ]
)
# python.org's shape: many short labels, but a fifth of the lines are prose
# and it carries a thousand words. Must NOT read as a shell.
NAV_HEAVY = "\n".join(
    [
        "Downloads",
        "Documentation",
        "Community",
        "Success Stories",
        "News",
        "Events",
        "About",
        "Jobs",
        "Donate",
        "Python 3.13",
        "Latest release",
        "Beginner's guide",
    ]
    * 3
    + [
        "Python is a programming language that lets you work quickly and integrate "
        "systems more effectively.",
        "Whether you are new to programming or an experienced developer, "
        "it is easy to learn and use.",
        "The mission of the Python Software Foundation is to promote, protect "
        "and advance the language.",
        "Conferences and workshops run on every continent, with recordings "
        "published for those who cannot attend.",
        "Package management, testing and packaging are all covered in the "
        "official tutorial series.",
        "Job boards list thousands of roles that name the language as a core requirement.",
        "The steering council publishes its decisions and the reasoning behind them in the open.",
        "Grants are available for community organisers, educators "
        "and open source maintainers alike.",
    ]
)


def _result(status: int, body: bytes = b"<html><body><p>x</p></body></html>") -> FetchResult:
    return FetchResult(
        url="https://example.test/page",
        status_code=status,
        headers={},
        body=body,
        content_type="text/html",
        tier="http",
        latency_ms=10,
        bytes_transferred=len(body),
    )


def _summary(
    markdown: str, words: int, chars: int | None = None, path: str = ""
) -> ExtractionSummary:
    return ExtractionSummary(
        markdown=markdown,
        word_count=words,
        char_count=len(markdown) if chars is None else chars,
        confidence=0.6,
        title="Page",
        extraction_path=path,
    )


# ------------------------------------------------------------- issue 1 -----


def test_the_scrape_envelope_is_strict_json_whatever_the_page_contains() -> None:
    """RFC 8259: control characters inside strings must be escaped.

    The engine always did this — the reporter's zsh `echo "$json"` turned `\\n`
    into a real newline at byte 59. Asserted here so it stays a guarantee.
    """
    markdown = '# Title\n\nLine one.\n\tIndented "quoted" text\x01 with a control byte.'
    payload = {"success": True, "data": {"markdown": markdown, "metadata": {"statusCode": 200}}}

    body = JSONResponse(content=jsonable_encoder(payload)).body

    assert not [b for b in body if b < 0x20], "raw control bytes in the envelope"
    parsed = json.loads(body.decode())  # strict by default, like Node's JSON.parse
    assert parsed["data"]["markdown"] == markdown


def test_the_scrape_route_does_not_hand_build_its_response() -> None:
    from engine.api.routes import scrape as route

    src = inspect.getsource(route)
    assert "Response(content=" not in src and "json.dumps(" not in src


# ------------------------------------------------------------- issue 3 -----


def test_a_202_with_no_body_is_not_a_page() -> None:
    verdict = validate(_result(202, body=b""))
    assert not verdict.ok
    assert verdict.reason == Reason.TARGET_ERROR
    assert verdict.signal == "status_202"
    assert verdict.details["status_code"] == 202


def test_a_202_that_carries_a_body_is_judged_on_the_body() -> None:
    assert validate(_result(202)).reason != Reason.TARGET_ERROR


def test_202_gets_one_more_look_before_it_fails() -> None:
    # Accepted-and-processing usually turns into a 200 a second later; a tier
    # climb would not help, a short retry does.
    assert "status_202" in TRANSIENT_STATUSES
    assert "status_503" in TRANSIENT_STATUSES


# ------------------------------------------------------------- issue 4 -----


def test_wait_for_forces_a_browser_because_nothing_else_can_wait() -> None:
    assert ScrapeOptions().forces_browser is False
    assert ScrapeOptions(waitFor=5000).forces_browser is True

    from engine.core.scrape_service import DomainProfile

    profile = DomainProfile("example.test")
    assert starting_tier(profile) in (Tier.HTTP, Tier.IMPERSONATE)
    assert starting_tier(profile, floor=Tier.BROWSER) == Tier.BROWSER


def test_wait_for_says_it_costs_a_browser() -> None:
    desc = ScrapeOptions.model_fields["waitFor"].description or ""
    assert "browser" in desc.lower()


# --------------------------------------------------------- issues 2 & 5 ----


def test_the_ancestry_shell_is_recognised_cold() -> None:
    assert is_nav_shell(SHELL, SHELL_WORDS)


def test_legitimate_pages_are_not_shells() -> None:
    assert not is_nav_shell(EXAMPLE_COM, len(EXAMPLE_COM.split()))
    assert not is_nav_shell(ARTICLE, len(ARTICLE.split()))
    # The hardest negative: nav-heavy but a real page. Word count alone should
    # clear it, and so should its share of long prose lines.
    assert not is_nav_shell(NAV_HEAVY, 1100)
    assert not is_nav_shell(NAV_HEAVY, len(NAV_HEAVY.split()))


def test_a_shell_is_a_shell_at_any_length() -> None:
    """The word cap is gone, and this is why.

    IMDb's homepage fell back to a 336-word dump of the menu and the language
    picker and was billed at 5 credits as content, because 336 was sixteen
    words over a 320 ceiling. Raising the ceiling would only have waited for a
    site with one more language. Shape decides now, and the same menu is a
    menu whether it carries 250 words or 2,500.
    """
    for words in (96, 250, 336, 2_500):
        assert is_nav_shell(SHELL, words), words
        assert is_nav_shell(SHELL, words, extraction_path="fallback"), words


IMDB_FALLBACK = "\n".join(
    [
        "Menu",
        "All",
        "All",
        "8 suggestions available",
        "Watchlist",
        "Sign in",
        "Sign in",
        "New customer?",
        "Create account",
        "EN",
        "Fully supported",
        "English (United States)",
        "Partially supported",
        "Some content may be auto-translated",
        "Français (Canada)",
        "Français (France)",
        "Deutsch (Deutschland)",
        "हिंदी (भारत)",
        "Italiano (Italia)",
        "Português (Brasil)",
        "Español (España)",
        "Español (México)",
        "Use app",
    ]
    * 5
)


def test_the_imdb_language_picker_is_a_shell() -> None:
    """Measured from a real capture: 125 lines, median 15 chars, no long
    lines, 0.8% sentences — a menu on every count, and it was sold as a page.
    """
    assert is_nav_shell(IMDB_FALLBACK, len(IMDB_FALLBACK.split()), extraction_path="fallback")


def test_the_imdb_content_capture_is_not_a_shell() -> None:
    """The negative control from the same site: the structured path returned
    real featured items at confidence 0.90 and must keep passing."""
    real = "\n".join(
        f"- **[A featured title number {i} with a genuine descriptive sentence "
        f"about it here](/title/tt{i:07d}/)** See the watch guide and more."
        for i in range(18)
    )
    assert not is_nav_shell(real, len(real.split()))


def test_empty_content_on_a_200_is_thin_not_success() -> None:
    verdict = validate(_result(200), extraction=_summary("", words=0, chars=0))
    assert not verdict.ok
    assert (verdict.reason, verdict.signal) == (Reason.THIN, "empty_content")


def test_a_nav_shell_on_a_200_is_thin_not_success() -> None:
    verdict = validate(_result(200), extraction=_summary(SHELL, words=SHELL_WORDS))
    assert not verdict.ok
    assert (verdict.reason, verdict.signal) == (Reason.THIN, "nav_shell")
    assert verdict.details["median"] <= 15


def test_a_real_article_still_passes() -> None:
    verdict = validate(_result(200), extraction=_summary(ARTICLE, words=len(ARTICLE.split())))
    assert verdict.ok, verdict


# ------------------------------------------------------------ wiring -------


def test_thin_climbs_a_tier_but_never_raises_the_floor() -> None:
    from engine.core import scrape_service

    src = inspect.getsource(scrape_service)

    # It escalates alongside soft blocks…
    assert "post_verdict.reason in (Reason.SOFT_BLOCK, Reason.BLOCKED, Reason.THIN)" in src

    # …but no line that writes a block to the domain profile knows about it.
    for line in src.splitlines():
        if "apply_block" in line or "last_block=" in line:
            assert "THIN" not in line, line

    # And it is not a reason to try another country.
    assert Reason.THIN not in scrape_service._GEO_RETRYABLE_REASONS


def test_thin_is_reported_as_extraction_failed_not_blocked() -> None:
    from engine.core.detect.validator import Verdict
    from engine.core.errors import ExtractionFailed
    from engine.core.scrape_service import _error_for

    err = _error_for(
        Verdict(ok=False, reason=Reason.THIN, signal="nav_shell", details={"word_count": 96}),
        ["http", "browser"],
    )
    assert isinstance(err, ExtractionFailed)
    assert err.detail["signal"] == "nav_shell"
    assert err.detail["tiers_attempted"] == ["http", "browser"]


def test_the_summary_carries_the_extraction_path() -> None:
    from engine.core import scrape_service

    assert "extraction_path=str(ext.extraction_path)" in inspect.getsource(scrape_service)


# ------------------------------------------------------- imdb, 9 Sep 2026 ---


def test_a_fallback_never_wins_by_returning_chrome() -> None:
    """More words only wins if the words are a page.

    Every rung of the heuristic chain chose on word count alone, so on IMDb's
    homepage a recall pass returning 324 words of nav and language picker beat
    the 167 words of real featured content before it. More words, no page,
    5 credits for a menu. 3 of 17 captures took a fallback that was strictly
    worse.
    """
    from engine.core.extract.heuristic import is_richer_extraction

    menu = "\n".join(["Menu", "Watchlist", "Sign in", "Create account", "EN"] * 30)
    content = "- **[A real featured item](/x)** with a line of description beside it."

    assert not is_richer_extraction(menu, content), "chrome won on word count"
    # And the ordinary case still works: more real words is better.
    assert is_richer_extraction(content + "\n" + content, content)
    assert not is_richer_extraction("", content)
    assert not is_richer_extraction(None, content)
