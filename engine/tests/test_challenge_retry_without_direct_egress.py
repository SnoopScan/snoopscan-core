"""The challenge retry must survive a deployment that forbids direct egress.

A challenge at a deep rung is retried on a FRESH exit, which works by dropping
the proxy that was refused — the deep rungs mint their own session per call.
But a request carrying no proxy is exactly what the egress policy refuses to
build, so on every deployment that forbids direct egress (which is every
production one) the retry raised DirectEgressRefused before a replacement exit
could be chosen, and the retry path never ran at all.
"""

from __future__ import annotations

import pytest

from engine.core.fetch.base import DirectEgressRefused, FetchRequest
from engine.settings import settings


@pytest.fixture
def no_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "allow_direct_egress", False)


def test_a_proxyless_request_is_still_refused_by_default(no_direct: None) -> None:
    """The policy itself must not be weakened: this is the chokepoint that
    stops thirty-odd call sites leaking the host's own address."""
    with pytest.raises(DirectEgressRefused):
        FetchRequest(url="https://example.com/page")


def test_a_rung_that_picks_its_own_exit_may_carry_no_proxy(no_direct: None) -> None:
    req = FetchRequest(url="https://example.com/page", exit_chosen_by_fetcher=True)
    assert req.proxy_url is None
    assert req.exit_chosen_by_fetcher


def test_the_flag_is_off_unless_asked_for(no_direct: None) -> None:
    """It must be opt-in, or it is just allow_direct_egress with extra steps."""
    with pytest.raises(DirectEgressRefused):
        FetchRequest(url="https://example.com/page", exit_chosen_by_fetcher=False)


def test_an_internal_host_is_still_exempt(no_direct: None) -> None:
    """SearXNG on loopback is our own service, not a target."""
    assert FetchRequest(url="http://127.0.0.1:8888/search").proxy_url is None
