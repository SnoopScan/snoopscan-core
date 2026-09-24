#!/usr/bin/env python3
"""Phase 1 checkpoint: measure the real tier-0/tier-1 success rate.

10-build-plan.md makes this a decision gate, not a report:

    "measure the tier-1 success rate across the real target set. If it clears
     70%, the browser tier is genuinely deferrable. If it is well below, bring
     Phase 4 forward."

Browser tiers are the most expensive phase to build AND to run (~40x tier 1),
so this number decides whether that work is justified at all.

It doubles as the weekly live smoke test 07-orchestration.md section 9 asks
for: track the pass rate over time, because a drop is early warning that a
library or a WAF changed before it shows up in production.

    python tools/checkpoint.py               # full target set
    python tools/checkpoint.py --group saas  # one group
    python tools/checkpoint.py --proxy       # route through the proxy
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from engine.core.errors import EngineError
from engine.core.extract.router import extract
from engine.core.fetch.base import FetchRequest
from engine.core.fetch.tier0_http import HttpFetcher
from engine.core.fetch.tier1_impersonate import ImpersonateFetcher

# The decision threshold from the build plan.
DEFERRABLE_THRESHOLD = 0.70

# Below this, a 200 carried almost nothing — the shape of a JS-rendered page
# fetched without a browser. Counted separately from an outright block,
# because it is the case a browser tier would actually fix.
THIN_CONTENT_WORDS = 200

# A target set weighted toward the real workload: lead-gen hits company sites
# and product directories, and the content pipeline hits articles and docs.
TARGETS: dict[str, list[str]] = {
    # Grouped by what people actually BUY a scraping API for, not by how hard
    # the site is. A set sorted by difficulty flatters whichever tier you are
    # trying to justify; a set sorted by use case tells you which customers we
    # can serve.
    #
    # RAG / LLM context from documentation is the single largest use case for
    # this category of product, so it is weighted accordingly.
    "docs": [
        "https://docs.python.org/3/library/asyncio.html",
        "https://developer.mozilla.org/en-US/docs/Web/HTTP",
        "https://www.postgresql.org/docs/current/sql-select.html",
        "https://redis.io/docs/latest/develop/",
        "https://docs.djangoproject.com/en/stable/topics/db/queries/",
        "https://fastapi.tiangolo.com/tutorial/first-steps/",
        "https://docs.pydantic.dev/latest/concepts/models/",
        "https://kubernetes.io/docs/concepts/workloads/pods/",
        "https://docs.docker.com/engine/reference/builder/",
        "https://nginx.org/en/docs/beginners_guide.html",
        "https://www.sqlite.org/lang_select.html",
        "https://caddyserver.com/docs/caddyfile",
    ],
    "api_docs": [
        "https://docs.stripe.com/api/charges",
        "https://developer.mozilla.org/en-US/docs/Web/API/fetch",
        "https://docs.github.com/en/rest/repos/repos",
        "https://www.twilio.com/docs/messaging/api",
        "https://docs.sentry.io/api/",
        "https://platform.openai.com/docs/api-reference/chat",
        "https://docs.anthropic.com/en/api/messages",
        "https://developers.cloudflare.com/api/",
    ],
    # Competitive pricing intelligence — a top-three commercial use case, and
    # the pages most likely to be JS-rendered behind a toggle.
    "pricing": [
        "https://stripe.com/gb/pricing",
        "https://www.twilio.com/en-us/pricing",
        "https://plausible.io/#pricing",
        "https://sentry.io/pricing/",
        "https://www.cloudflare.com/plans/",
        "https://vercel.com/pricing",
        "https://www.digitalocean.com/pricing",
        "https://render.com/pricing",
        "https://supabase.com/pricing",
        "https://www.heroku.com/pricing",
    ],
    "saas": [
        "https://stripe.com/gb",
        "https://www.twilio.com/en-us",
        "https://sentry.io/welcome/",
        "https://plausible.io/",
        "https://www.fastmail.com/",
        "https://tailscale.com/",
        "https://linear.app/",
        "https://www.notion.com/",
        "https://vercel.com/",
        "https://supabase.com/",
    ],
    # Product monitoring — customers diff these on a schedule.
    "changelogs": [
        "https://github.blog/changelog/",
        "https://vercel.com/changelog",
        "https://www.docker.com/blog/",
        "https://about.gitlab.com/releases/",
        "https://tailscale.com/changelog",
    ],
    "news": [
        "https://www.theverge.com/tech",
        "https://techcrunch.com/",
        "https://www.zdnet.com/",
        "https://arstechnica.com/",
        "https://www.bbc.co.uk/news/technology",
        "https://www.reuters.com/technology/",
        "https://apnews.com/hub/technology",
        "https://www.wired.com/category/business/",
        "https://www.engadget.com/",
        "https://www.theregister.com/",
    ],
    "blogs": [
        "https://blog.cloudflare.com/",
        "https://netflixtechblog.com/",
        "https://engineering.fb.com/",
        "https://stackoverflow.blog/",
        "https://blog.rust-lang.org/",
        "https://simonwillison.net/",
        "https://danluu.com/",
        "https://martinfowler.com/articles/",
    ],
    "reference": [
        "https://en.wikipedia.org/wiki/Web_scraping",
        "https://en.wikipedia.org/wiki/Transmission_Control_Protocol",
        "https://www.gov.uk/browse/business",
        "https://www.rfc-editor.org/rfc/rfc9110.html",
        "https://www.w3.org/TR/WCAG21/",
        "https://schema.org/Product",
    ],
    "directories": [
        "https://news.ycombinator.com/",
        "https://news.ycombinator.com/show",
        "https://alternativeto.net/",
        "https://slashdot.org/",
        "https://www.producthunt.com/",
        "https://awesome-selfhosted.net/",
        "https://landscape.cncf.io/",
        "https://pypi.org/search/?q=scraping",
    ],
    "forums": [
        "https://news.ycombinator.com/item?id=1",
        "https://lobste.rs/",
        "https://stackoverflow.com/questions",
        "https://forum.djangoproject.com/",
        "https://discuss.python.org/",
        "https://users.rust-lang.org/",
    ],
    "ecommerce": [
        "https://www.etsy.com/",
        "https://world.openfoodfacts.org/",
        "https://www.ebay.co.uk/",
        "https://www.johnlewis.com/",
        "https://www.argos.co.uk/",
        "https://shop.tesla.com/",
        "https://www.ikea.com/gb/en/",
    ],
    "jobs": [
        "https://weworkremotely.com/",
        "https://remoteok.com/",
        "https://news.ycombinator.com/jobs",
        "https://boards.greenhouse.io/",
        "https://jobs.lever.co/",
    ],
    # Lead-gen contact discovery runs against pages exactly like these.
    "company": [
        "https://www.cloudflare.com/en-gb/about-overview/",
        "https://stripe.com/gb/contact",
        "https://www.mongodb.com/company/contact",
        "https://basecamp.com/about",
        "https://about.gitlab.com/company/",
        "https://www.atlassian.com/company/contact",
    ],
    "listings": [
        "https://www.rightmove.co.uk/",
        "https://www.zoopla.co.uk/",
        "https://www.skyscanner.net/",
        "https://www.trainline.com/",
    ],
}


@dataclass
class Probe:
    url: str
    group: str
    tier: str | None = None
    status: int | None = None
    page_type: str = "-"
    words: int = 0
    confidence: float = 0.0
    bytes_transferred: int = 0
    latency_ms: int = 0
    outcome: str = "failed"  # ok | thin | blocked | error
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.outcome == "ok"


@dataclass
class Report:
    probes: list[Probe] = field(default_factory=list)

    def rate(self, *outcomes: str) -> float:
        if not self.probes:
            return 0.0
        hits = sum(1 for p in self.probes if p.outcome in outcomes)
        return hits / len(self.probes)

    @property
    def tier_mix(self) -> Counter[str]:
        return Counter(p.tier or "none" for p in self.probes if p.usable)


async def warm_url(url: str, group: str) -> Probe:
    """One URL through the FULL pipeline, so the sweep BUILDS the intelligence.

    `probe_url` below deliberately bypasses ScrapeService to measure what a
    cold request gets. That is right for a measurement, and it meant a 105-URL
    sweep taught `domain_profiles` precisely nothing — we had a measurement
    path and a learning path that never met.

    This is the other half: escalation memory, WAF detection, politeness
    delays, working country and proxy scores are all written as a side effect
    of scraping. Run it on a schedule and the sweep stops being a report and
    starts being the asset — every domain we touch is cheaper next time.

    Needs a database. `probe_url` does not.
    """
    from engine.api.deps import get_fetchers
    from engine.core.models import ScrapeOptions
    from engine.core.scrape_service import ScrapeService

    result = Probe(url=url, group=group)
    service = ScrapeService(get_fetchers(), persist=True)
    started = time.monotonic()
    try:
        outcome = await service.scrape(url, ScrapeOptions(maxAge=0))
    except EngineError as exc:
        result.outcome = "blocked" if exc.code.name.startswith("BLOCK") else "error"
        result.detail = f"{exc.code.name}"
        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result
    except Exception as exc:  # noqa: BLE001 - a warm sweep must not stop on one URL
        result.outcome = "error"
        result.detail = type(exc).__name__
        return result

    meta = outcome.data.metadata
    result.tier = getattr(meta, "tier", None)
    result.status = getattr(meta, "statusCode", None)
    result.page_type = str(getattr(meta, "pageType", "-") or "-")
    result.words = len((outcome.data.markdown or "").split())
    result.confidence = round(float(getattr(meta, "extractionConfidence", 0.0) or 0.0), 2)
    result.latency_ms = int((time.monotonic() - started) * 1000)
    result.outcome = "ok" if result.words >= THIN_CONTENT_WORDS else "thin"
    if result.outcome == "thin":
        result.detail = f"{result.words} words"
    return result


async def probe_url(url: str, group: str, use_proxy: bool) -> Probe:
    """One URL, tier 0 then tier 1, stopping at the first usable result.

    Deliberately does not run the full ScrapeService: this measures the FETCH
    tiers, so caching, politeness memory and domain profiles must not mask
    what a cold request actually gets.
    """
    result = Probe(url=url, group=group)
    # Deferred: the checkpoint is public tooling and must run in a deployment
    # that has no proxy layer installed. --proxy simply does nothing there.
    proxy = None
    if use_proxy:
        try:
            from engine.core.proxy.vendor import residential

            proxy = residential()
        except ImportError:
            proxy = None

    for tier_name, fetcher in (
        ("http", HttpFetcher()),
        ("impersonate", ImpersonateFetcher()),
    ):
        request = FetchRequest(
            url=url,
            timeout_ms=30_000,
            proxy_url=proxy.connection_url() if proxy else None,
            proxy_id=proxy.id if proxy else None,
            proxy_type=str(proxy.type) if proxy else None,
        )
        try:
            fetched = await fetcher.fetch(request)
        except EngineError as exc:
            result.outcome, result.detail = "error", exc.message
            continue
        finally:
            if hasattr(fetcher, "aclose"):
                await fetcher.aclose()

        result.tier = tier_name
        result.status = fetched.status_code
        result.bytes_transferred = fetched.bytes_transferred
        result.latency_ms = fetched.latency_ms

        if fetched.error:
            result.outcome, result.detail = "error", fetched.error[:60]
            continue
        if fetched.status_code != 200:
            result.outcome = "blocked" if fetched.status_code in (403, 429) else "error"
            result.detail = f"HTTP {fetched.status_code}"
            continue

        extraction = extract(fetched.text(), fetched.url)
        result.page_type = str(extraction.page_type)
        result.words = extraction.word_count
        result.confidence = round(extraction.confidence, 2)

        if extraction.word_count >= THIN_CONTENT_WORDS:
            result.outcome, result.detail = "ok", ""
            return result

        # A 200 with almost no text is the JS-rendered case a browser fixes.
        result.outcome = "thin"
        result.detail = f"{extraction.word_count} words"

    return result


async def run(groups: list[str], use_proxy: bool, as_json: bool, warm: bool = False) -> int:
    selected = {g: TARGETS[g] for g in groups if g in TARGETS} or TARGETS
    report = Report()
    started = time.monotonic()

    if not as_json:
        print(
            f"{'group':<13} {'tier':<12} {'outcome':<9} {'type':<9} {'words':>6} {'conf':>5}  url"
        )
        print("-" * 108)

    for group, urls in selected.items():
        for url in urls:
            probe = await (warm_url(url, group) if warm else probe_url(url, group, use_proxy))
            report.probes.append(probe)
            if not as_json:
                print(
                    f"{group:<13} {probe.tier or '-':<12} {probe.outcome:<9} "
                    f"{probe.page_type:<9} {probe.words:>6} {probe.confidence:>5}  "
                    f"{probe.url[:44]}" + (f"  ({probe.detail})" if probe.detail else "")
                )
            # Politeness between distinct hosts as well as within one.
            await asyncio.sleep(1.2)

    elapsed = time.monotonic() - started
    usable = report.rate("ok")
    thin = report.rate("thin")
    blocked = report.rate("blocked")

    if as_json:
        print(
            json.dumps(
                {
                    "usable_rate": round(usable, 3),
                    "thin_rate": round(thin, 3),
                    "blocked_rate": round(blocked, 3),
                    "probes": [p.__dict__ for p in report.probes],
                },
                indent=2,
            )
        )
        return 0

    total = len(report.probes)
    print("\n" + "=" * 108)
    print(f"{total} targets in {elapsed:.0f}s")
    print(f"  usable at tier 0/1 : {usable:.0%}  ({sum(1 for p in report.probes if p.usable)})")
    print(
        f"  thin (JS-rendered) : {thin:.0%}  "
        f"({sum(1 for p in report.probes if p.outcome == 'thin')})"
    )
    print(
        f"  blocked            : {blocked:.0%}  "
        f"({sum(1 for p in report.probes if p.outcome == 'blocked')})"
    )
    print(f"  errored            : {report.rate('error'):.0%}")

    if report.tier_mix:
        print("\n  tier that succeeded:", dict(report.tier_mix))

    ok_probes = [p for p in report.probes if p.usable]
    if ok_probes:
        mean_conf = sum(p.confidence for p in ok_probes) / len(ok_probes)
        mean_kb = sum(p.bytes_transferred for p in ok_probes) / len(ok_probes) / 1024
        print(f"  mean extraction confidence: {mean_conf:.2f}")
        print(f"  mean transfer per page:     {mean_kb:.0f}KB")

    print("\n  by group:")
    for group in selected:
        group_probes = [p for p in report.probes if p.group == group]
        if group_probes:
            rate = sum(1 for p in group_probes if p.usable) / len(group_probes)
            print(f"    {group:<13} {rate:.0%}")

    print("\n" + "-" * 108)
    print("VERDICT")
    if usable >= DEFERRABLE_THRESHOLD:
        print(
            f"  {usable:.0%} usable at tier 0/1, at or above the {DEFERRABLE_THRESHOLD:.0%} "
            f"threshold."
        )
        print("  The browser tier is DEFERRABLE. Phase 5 (extraction quality) is the")
        print("  better next investment — it is where the differentiation is, and it")
        print("  costs nothing per request to run.")
    else:
        print(f"  {usable:.0%} usable at tier 0/1, BELOW the {DEFERRABLE_THRESHOLD:.0%} threshold.")
        print("  Bring Phase 4's browser tiers forward. Note how much of the shortfall")
        print("  is 'thin' rather than 'blocked': thin is what a browser fixes, blocked")
        print("  needs stealth and a residential IP.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1 checkpoint")
    parser.add_argument("--group", action="append", default=[], help="limit to a group")
    parser.add_argument("--proxy", action="store_true", help="route through the proxy")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--warm",
        action="store_true",
        help="run through the full pipeline so the sweep BUILDS domain intelligence "
        "(needs a database); without it the run only measures",
    )
    args = parser.parse_args()
    _ = Path
    return asyncio.run(run(args.group, args.proxy, args.json, args.warm))


if __name__ == "__main__":
    sys.exit(main())
