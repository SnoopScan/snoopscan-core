"""A climb chases a block or a content-quality complaint; it must not throw
away a richer rung it already had along the way.

Reported from real use (a tester, 15 Sep 2026): a news site's raw HTML
carries the full article — genuinely, for any anonymous request — but the
page also reads as THIN at the cheap tier for an unrelated reason (a heavy
page, low text-to-markup ratio). Auto-escalation climbs to the browser tier,
which executes the site's own client-side paywall script; that script
replaces the article with a subscribe teaser. The rendered page still has
real nav/search furniture, so the near-empty and link-only gates never fire
on it, and it validates as `ok` — with two words in it. Before the fix, the
engine kept that as the "successful" answer and threw away the cheap tier's
41 real words, because escalation only ever asked "did this rung validate?"
and never asked "does it actually have less than what the last rung had?"
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.core.fetch.base import FetchRequest, FetchResult
from engine.core.models import ScrapeOptions, Tier
from engine.core.scrape_service import ScrapeService

# Real article prose at the cheap tier — varied enough to clear the
# plausibility/decoy layer, but proportionally thin against the padded raw
# HTML below it, which is exactly what a heavy, ad-laden real page looks
# like to the near-empty check.
_ARTICLE = (
    "Ciro and Carmine run a small cafe on the Horsefair and say the new bus "
    "changes have cut footfall this week, with regulars still stopping by most mornings."
)
HTTP_THIN_BUT_REAL = (
    b"<html><head><title>Cafe Story</title></head><body><article><h1>Cafe Story</h1><p>"
    + _ARTICLE.encode()
    + b"</p></article><script>window.__DATA__="
    + b"x" * 12_000
    + b";</script></body></html>"
)

# The browser tier's rendered DOM after the site's own paywall script ran:
# real furniture (nav, a search box, plenty of links) surrounds almost no
# article text — which is exactly why it validates `ok` despite carrying
# far less content than the tier below it.
_NAV_LINKS = "".join(f"<a href='/l{i}'>Link {i}</a>" for i in range(30))
BROWSER_PAYWALL_TEASER = (
    b"<html><head><title>Cafe Story</title></head><body>"
    b"<nav><input type='search' name='q'>" + _NAV_LINKS.encode() + b"</nav>"
    b"<article><h1>Cafe Story</h1>"
    b"<p>Subscribe now to keep reading this exclusive story.</p></article>"
    b"</body></html>"
)


@dataclass
class Scripted:
    name: str
    body: bytes
    calls: int = 0

    async def fetch(self, req: FetchRequest) -> FetchResult:
        self.calls += 1
        return FetchResult(
            url=req.url,
            status_code=200,
            headers={"content-type": "text/html"},
            body=self.body,
            content_type="text/html",
            tier=self.name,
            latency_ms=50,
            bytes_transferred=len(self.body),
        )

    async def healthcheck(self) -> bool:
        return True


async def test_a_browser_tier_paywall_teaser_does_not_overwrite_a_richer_http_tier() -> None:
    http = Scripted("http", HTTP_THIN_BUT_REAL)
    browser = Scripted("browser", BROWSER_PAYWALL_TEASER)
    service = ScrapeService({Tier.HTTP: http, Tier.BROWSER: browser}, persist=False)

    out = await service.scrape("https://example.com/story", ScrapeOptions())

    assert http.calls == 1
    assert browser.calls == 1, "the thin http result must still trigger a climb"
    # The climb happened (browser was tried), but the richer rung's content
    # is what gets returned — not the two-word teaser the browser tier
    # rendered, even though the teaser is the one that "validated".
    assert "Ciro and Carmine" in out.data.markdown
    assert "Subscribe now" not in out.data.markdown
    assert out.data.cost.tier == "http"
