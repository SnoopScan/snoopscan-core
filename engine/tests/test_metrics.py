"""Metrics, and the cardinality rule that keeps them usable.

The failure this guards against is specific: Prometheus creates one time
series per label combination, so a `domain` label on a crawler touching
thousands of hosts degrades the scrape, then the storage, then takes the
monitoring down at exactly the moment you need it.
"""

from __future__ import annotations

import pytest
from prometheus_client import generate_latest

from engine.core import metrics

# Every label a metric may carry. Each must have small, fixed cardinality:
# a tier is one of six, an outcome one of five, a page type one of seven.
ALLOWED_LABELS = {
    "tier",
    "outcome",
    "signal",
    "page_type",
    "path",
    "proxy_type",
    "result",
    "queue",
    "kind",
}

# Explicitly banned. Domain-level attribution lives in Postgres, where
# `fetch_log` and `proxy_usage` already hold it and can be queried without
# bound.
UNBOUNDED_LABELS = {"domain", "url", "host", "job_id", "page_id", "api_key"}


def collectors() -> list[object]:
    return [
        metrics.fetch_attempts,
        metrics.fetch_duration,
        metrics.escalation_depth,
        metrics.blocks,
        metrics.plausibility_rejections,
        metrics.extraction_confidence,
        metrics.extraction_path,
        metrics.proxy_bytes,
        metrics.browser_time,
        metrics.cache_hits,
        metrics.queue_depth,
        metrics.job_duration,
    ]


def test_no_metric_carries_an_unbounded_label() -> None:
    """A domain label is a cardinality explosion, not an observability win."""
    for collector in collectors():
        names = set(getattr(collector, "_labelnames", ()))
        offending = names & UNBOUNDED_LABELS
        assert not offending, (
            f"{collector._name} labels by {offending} — one time series per "
            f"value will take the monitoring down"
        )


def test_every_label_is_from_the_allowed_set() -> None:
    for collector in collectors():
        for label in getattr(collector, "_labelnames", ()):
            assert label in ALLOWED_LABELS, (
                f"{collector._name} uses label {label!r}; confirm its cardinality "
                f"is bounded before adding it to ALLOWED_LABELS"
            )


def test_every_metric_carries_a_description() -> None:
    """A metric nobody can interpret at 3am is not observability."""
    for collector in collectors():
        documentation = getattr(collector, "_documentation", "")
        assert len(documentation) > 30, f"{collector._name} has no useful help text"


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def test_recording_an_attempt_emits_both_count_and_duration() -> None:
    metrics.record_attempt("http", "success", 0.42)
    output = generate_latest().decode()
    assert 'engine_fetch_attempts_total{outcome="success",tier="http"}' in output
    assert "engine_fetch_duration_seconds_bucket" in output


def test_blocks_are_labelled_by_the_signal_that_fired() -> None:
    """The verdict MIX shifting is the earliest warning a target changed, so
    the signal has to survive into the metric."""
    metrics.record_block("impersonate", "cf_challenge")
    output = generate_latest().decode()
    assert 'signal="cf_challenge"' in output


def test_a_missing_signal_does_not_produce_an_empty_label() -> None:
    metrics.record_block("http", None)
    assert 'signal="unknown"' in generate_latest().decode()


def test_extraction_confidence_is_recorded_per_page_type() -> None:
    """A falling mean is a silent regression, and it is worth knowing WHICH
    page type is falling."""
    metrics.record_extraction("forum", "structured", 0.88)
    output = generate_latest().decode()
    assert 'engine_extraction_confidence_count{page_type="forum"}' in output
    assert 'path="structured"' in output


def test_cost_is_only_recorded_when_it_was_incurred() -> None:
    """A direct fetch must not appear as zero proxy bytes — that would dilute
    the per-page cost figure the budget depends on."""
    before = generate_latest().decode()
    metrics.record_cost(None, 0, 0)
    assert generate_latest().decode().count("engine_proxy_bytes_total") == before.count(
        "engine_proxy_bytes_total"
    )


def test_proxy_cost_is_recorded_when_it_is_incurred() -> None:
    metrics.record_cost("residential", 150_000, 0)
    assert 'proxy_type="residential"' in generate_latest().decode()


def test_cache_outcomes_are_both_counted() -> None:
    """Only counting hits makes the hit RATE unknowable."""
    metrics.record_cache(True)
    metrics.record_cache(False)
    output = generate_latest().decode()
    assert 'result="hit"' in output
    assert 'result="miss"' in output


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_escalation_depth_is_observed(depth: int) -> None:
    metrics.record_escalation(depth)
    assert "engine_escalation_depth_sum" in generate_latest().decode()


async def test_a_scrape_populates_the_engine_metrics() -> None:
    """End to end: the endpoint should describe real work, not just Python."""
    from engine.core.fetch.base import FetchRequest, FetchResult
    from engine.core.models import ScrapeOptions, Tier
    from engine.core.scrape_service import ScrapeService
    from engine.tests.fixtures.builders import article_html

    class Stub:
        name = "http"

        async def fetch(self, req: FetchRequest) -> FetchResult:
            body = article_html().encode()
            return FetchResult(
                url=req.url,
                status_code=200,
                headers={},
                body=body,
                content_type="text/html",
                tier="http",
                latency_ms=30,
                bytes_transferred=len(body),
            )

        async def healthcheck(self) -> bool:
            return True

    service = ScrapeService({Tier.HTTP: Stub()}, persist=False)
    await service.scrape("https://example.com/post", ScrapeOptions(maxAge=0))

    output = generate_latest().decode()
    assert "engine_extraction_confidence_count" in output
    assert "engine_escalation_depth_count" in output
