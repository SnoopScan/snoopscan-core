"""The open core must scrape with the proxy layer absent.

The published core ships without engine/core/proxy — it is the proprietary
side — and every place the scrape path reaches for it has to degrade. Three
did not: recording which provider carried each attempt, checking whether a
provider had refused a target, and retrying a blocked domain from other
countries. In the export, the first crawl that reached any of them died on
"No module named 'engine.core.proxy'" — found by running the exported suite
on its own (Sep 2026), which is the only place the absence is real.

The layer is hidden here the way the export hides it: the import fails.
"""

from __future__ import annotations

import sys

import pytest

from engine.core.detect.validator import Reason, Verdict
from engine.core.fetch.escalation import Attempt, DomainProfile, EscalationOutcome
from engine.core.models import ScrapeOptions, Tier
from engine.core.scrape_service import ScrapeService, _hit_a_refusal


@pytest.fixture
def no_proxy_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(sys.modules):
        if name == "engine.core.proxy" or name.startswith("engine.core.proxy."):
            monkeypatch.delitem(sys.modules, name)
    # None in sys.modules makes `import` raise ImportError, as a missing
    # package does in the published tree.
    for name in (
        "engine.core.proxy",
        "engine.core.proxy.providers",
        "engine.core.proxy.pool",
        "engine.core.proxy.vendor",
        "engine.core.proxy.budget",
    ):
        monkeypatch.setitem(sys.modules, name, None)


def _refused_outcome() -> EscalationOutcome:
    blocked = Verdict(ok=False, reason=Reason.BLOCKED, signal="status_403")
    return EscalationOutcome(
        result=None,
        verdict=blocked,
        attempts=[
            Attempt(
                tier=Tier.IMPERSONATE,
                verdict=blocked,
                latency_ms=10,
                status_code=None,
                bytes_transferred=0,
                error="ProxyError: 403",
                proxy_id="res-prov_x-us",
            )
        ],
    )


async def test_recording_provider_health_is_skipped_not_fatal(no_proxy_layer: None) -> None:
    await ScrapeService({}, persist=False)._note_providers(_refused_outcome(), "example.com")


def test_no_proxy_layer_means_no_provider_refused(no_proxy_layer: None) -> None:
    assert _hit_a_refusal(_refused_outcome()) is False


async def test_retrying_other_countries_is_declined_not_fatal(no_proxy_layer: None) -> None:
    rescued = await ScrapeService({}, persist=False)._retry_other_countries(
        "https://example.com/",
        None,
        ScrapeOptions(),
        DomainProfile("example.com"),
        "example.com",
        Tier.IMPERSONATE,
    )
    assert rescued is None
