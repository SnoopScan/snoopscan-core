"""A domain learned to need a proxy must be able to un-learn it.

25 Sep 2026: the flag was permanent. One rate-limit or timeout followed by a
success through a stealth rung's own exit, and the domain paid residential
bandwidth on every request after. 41 of 75 flagged domains had never been
blocked; the official MCP registry (464 successes, 3 blocks) was proxied on
every call.
"""

from __future__ import annotations

from engine.core.fetch.escalation import DomainProfile
from engine.core.scrape_service import due_direct_reprobe, forget_proxy_need
from engine.settings import settings


def test_a_flagged_domain_is_tried_direct_every_nth_request(monkeypatch) -> None:
    monkeypatch.setattr(settings, "proxy_direct_reprobe_every", 20)
    flagged = DomainProfile("example.com", requires_proxy=True)
    due = [n for n in range(1, 61) if due_direct_reprobe(_with(flagged, success_count=n))]
    assert due == [20, 40, 60]


def test_an_unflagged_domain_is_never_reprobed(monkeypatch) -> None:
    monkeypatch.setattr(settings, "proxy_direct_reprobe_every", 20)
    assert not due_direct_reprobe(DomainProfile("example.com", success_count=20))


def test_reprobing_can_be_switched_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "proxy_direct_reprobe_every", 0)
    flagged = DomainProfile("example.com", requires_proxy=True, success_count=20)
    assert not due_direct_reprobe(flagged)


def test_a_direct_success_clears_the_flag() -> None:
    assert forget_proxy_need(DomainProfile("example.com", requires_proxy=True), proxy_id=None)


def test_a_proxied_success_keeps_the_flag() -> None:
    flagged = DomainProfile("example.com", requires_proxy=True)
    assert not forget_proxy_need(flagged, proxy_id="res-us")


def test_an_unflagged_domain_has_nothing_to_forget() -> None:
    assert not forget_proxy_need(DomainProfile("example.com"), proxy_id=None)


def _with(profile: DomainProfile, **changes: int) -> DomainProfile:
    from dataclasses import replace

    return replace(profile, **changes)
