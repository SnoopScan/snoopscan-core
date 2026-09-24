#!/usr/bin/env python3
"""Live smoke test against real targets.

Not part of the unit suite — it needs network. Run it weekly (07-orchestration
section 9) and track the pass rate over time: a drop is early warning that a
library or a WAF changed before the failure shows up in production.

    python tools/smoke.py            # default target set
    python tools/smoke.py URL ...    # specific URLs
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from engine.core.fetch.tier0_http import HttpFetcher
from engine.core.fetch.tier1_impersonate import ImpersonateFetcher
from engine.core.models import ScrapeOptions, Tier
from engine.core.scrape_service import ScrapeService

# Neutral, stable, publicly documented endpoints. No identifying targets.
DEFAULT_TARGETS = [
    "https://example.com/",
    "https://www.iana.org/help/example-domains",
    "https://httpbin.org/html",
    "https://en.wikipedia.org/wiki/Web_scraping",
    "https://news.ycombinator.com/",
]


async def run(urls: list[str], persist: bool) -> int:
    service = ScrapeService(
        {Tier.HTTP: HttpFetcher(), Tier.IMPERSONATE: ImpersonateFetcher()},
        persist=persist,
    )
    options = ScrapeOptions(maxAge=0, storeInCache=persist)

    passed = 0
    failed = 0
    print(f"{'result':<8} {'tier':<12} {'type':<9} {'words':>6} {'conf':>5}  url")
    print("-" * 92)

    for url in urls:
        started = time.monotonic()
        try:
            outcome = await service.scrape(url, options)
        except Exception as exc:  # noqa: BLE001 - a smoke run reports, never crashes
            failed += 1
            print(f"{'FAIL':<8} {'-':<12} {'-':<9} {'-':>6} {'-':>5}  {url}")
            print(f"         {type(exc).__name__}: {exc}")
            continue

        elapsed = int((time.monotonic() - started) * 1000)
        meta = outcome.data.metadata
        cost = outcome.data.cost
        passed += 1
        print(
            f"{'ok':<8} {cost.tier or '-':<12} {meta.pageType:<9} "
            f"{meta.wordCount:>6} {meta.extractionConfidence:>5.2f}  {url}  ({elapsed}ms)"
        )
        if cost.tiers_attempted != [cost.tier]:
            print(f"         escalated through: {' -> '.join(cost.tiers_attempted)}")

    total = passed + failed
    rate = (passed / total * 100) if total else 0.0
    print("-" * 92)
    print(f"{passed}/{total} passed ({rate:.0f}%)")
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Live smoke test")
    parser.add_argument("urls", nargs="*", help="URLs to scrape (default: built-in set)")
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Write to Postgres and use domain profiles (requires a database)",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.urls or DEFAULT_TARGETS, args.persist))


if __name__ == "__main__":
    sys.exit(main())
