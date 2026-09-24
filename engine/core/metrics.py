"""Prometheus metrics (07-orchestration.md section 10).

One deliberate deviation from the spec's list, and it matters:

The spec labels several metrics by `domain`. Prometheus creates one time
series per label combination, so a domain label on a crawler that touches
thousands of hosts is an unbounded cardinality explosion — it degrades the
scrape, then the storage, then takes the monitoring down at exactly the moment
you need it. Domain-level attribution belongs in Postgres, where `fetch_log`
and `proxy_usage` already hold it and can be queried without bound.

So metrics here are labelled by things with SMALL fixed cardinality — tier,
outcome, signal, queue — and the per-domain question is answered by a SQL
query instead. `fetch_log` is written per tier attempt precisely so that
remains possible.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

fetch_attempts = Counter(
    "engine_fetch_attempts_total",
    "Fetch attempts, one per TIER attempt rather than per request, so escalation waste is visible.",
    ["tier", "outcome"],
)

fetch_duration = Histogram(
    "engine_fetch_duration_seconds",
    "Wall-clock time per fetch attempt.",
    ["tier"],
    # Tier 0 answers in well under a second; a browser tier takes tens. One
    # bucket set has to cover both without losing the fast end.
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 45, 90),
)

escalation_depth = Histogram(
    "engine_escalation_depth",
    "How many tiers a request needed. A rising average means we are paying "
    "more per page and something changed.",
    buckets=(1, 2, 3, 4, 5, 6),
)

# --------------------------------------------------------------------------
# Blocking
# --------------------------------------------------------------------------

blocks = Counter(
    "engine_blocks_total",
    "Blocks and soft blocks by the signal that fired. The verdict MIX "
    "shifting is the earliest warning a target changed.",
    ["tier", "signal"],
)

plausibility_rejections = Counter(
    "engine_plausibility_rejections_total",
    "Content refused as implausible. A spike is either a decoy campaign or a "
    "broken threshold, and the two need opposite responses.",
    ["signal"],
)

# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

extraction_confidence = Histogram(
    "engine_extraction_confidence",
    "Extraction confidence per page. A falling mean is a silent regression — "
    "the output still looks like text.",
    ["page_type"],
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

extraction_path = Counter(
    "engine_extraction_path_total",
    "Which extractor produced the output. A shift toward `fallback` means "
    "the routed path is failing without anything erroring.",
    ["path", "page_type"],
)

# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------

proxy_bytes = Counter(
    "engine_proxy_bytes_total",
    "Measured proxy bandwidth. The dominant real cost.",
    ["proxy_type"],
)

browser_time = Counter(
    "engine_browser_ms_total",
    "Wall-clock milliseconds a browser was held. Browser time is the second "
    "largest cost after proxy bandwidth.",
)

cache_hits = Counter(
    "engine_cache_total",
    "Cache hits and misses. A falling hit rate means rising spend.",
    ["result"],
)

# A ratio rather than a byte count, because the cap is configurable: an alert
# written against absolute bytes needs editing every time the budget changes,
# and an alert nobody updates is one that fires at the wrong number.
#
# The gauge is defined here in the public core while the budget logic that
# populates it is proprietary. That is deliberate — a self-hoster running
# without the proxy layer sees this sit at zero, which is accurate.
proxy_budget_used = Gauge(
    "engine_proxy_budget_used_ratio",
    "Proxy bandwidth spent as a fraction of the daily cap. At 1.0 the engine "
    "refuses new proxied requests and continues direct, which is a designed "
    "degradation rather than a failure — but somebody needs to know.",
)

# --------------------------------------------------------------------------
# Queue and workers
# --------------------------------------------------------------------------

queue_depth = Gauge(
    "engine_queue_depth",
    "Items waiting per queue. Growth over 30 minutes is the alert.",
    ["queue"],
)

browser_pool_size = Gauge("engine_browser_pool_size", "Browsers currently launched in the pool.")
browser_pool_in_use = Gauge(
    "engine_browser_pool_in_use",
    "Browsers checked out right now. Sustained equality with pool size means "
    "requests are queueing for a browser.",
)

job_duration = Histogram(
    "engine_job_duration_seconds",
    "End-to-end job duration by kind. A crawl slowing without its page count "
    "rising means the targets got harder, not the job bigger.",
    ["kind"],
    buckets=(1, 5, 15, 60, 300, 900, 3600),
)

# --------------------------------------------------------------------------
# Recording helpers
# --------------------------------------------------------------------------


def record_attempt(tier: str, outcome: str, seconds: float) -> None:
    fetch_attempts.labels(tier=tier, outcome=outcome).inc()
    fetch_duration.labels(tier=tier).observe(seconds)


def record_block(tier: str, signal: str | None) -> None:
    blocks.labels(tier=tier, signal=signal or "unknown").inc()


def record_extraction(page_type: str, path: str, confidence: float) -> None:
    extraction_confidence.labels(page_type=page_type).observe(confidence)
    extraction_path.labels(path=path, page_type=page_type).inc()


def record_cost(proxy_type: str | None, proxy_bytes_used: int, browser_ms: int) -> None:
    if proxy_bytes_used and proxy_type:
        proxy_bytes.labels(proxy_type=proxy_type).inc(proxy_bytes_used)
    if browser_ms:
        browser_time.inc(browser_ms)


def record_cache(hit: bool) -> None:
    cache_hits.labels(result="hit" if hit else "miss").inc()


def record_escalation(tiers_attempted: int) -> None:
    escalation_depth.observe(tiers_attempted)
