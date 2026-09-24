"""An option the answering rung cannot honour must not be silently dropped.

Two live measurements against httpbin.org/headers, 9 Sep 2026:

    mobile: true          the User-Agent was BYTE-IDENTICAL to the desktop one
    location: {"de"}      no Accept-Language header at all

Both were accepted, both were billed, both did nothing. `mobile` was wired to
the browser rungs only, and tier 0 — where most requests are answered — has no
phone to be. `location` was read at tier 0 as `location.languages`, a field the
public API does not document; a caller who set only `country`, which is what
the API DOES document, got the language header one rung up and nothing here.

The same fault, twice, with the same shape as `waitFor` before it: an option
the rung cannot serve, dropped without a word. The general fix is a floor —
a request declares the cheapest rung that can honour it, and the ladder starts
no lower.
"""

from __future__ import annotations

import httpx
import pytest

from engine.core.fetch.base import FetchRequest
from engine.core.fetch.tier0_http import HttpFetcher
from engine.core.models import Location, ScrapeOptions, Tier
from engine.core.scrape_service import DomainProfile

# -- the floor ---------------------------------------------------------------


def test_a_phone_request_will_not_be_answered_by_the_bot_identity_rung() -> None:
    from engine.core.fetch.escalation import starting_tier

    assert ScrapeOptions().tier_floor is None
    assert ScrapeOptions(mobile=True).tier_floor == Tier.IMPERSONATE
    # A browser need outranks it; the floor is the highest thing asked for.
    assert ScrapeOptions(mobile=True, waitFor=1000).tier_floor == Tier.BROWSER

    profile = DomainProfile("example.test")
    assert starting_tier(profile) == Tier.HTTP
    assert starting_tier(profile, floor=Tier.IMPERSONATE) == Tier.IMPERSONATE


def test_the_floor_never_pulls_a_request_down_a_rung() -> None:
    """A domain that has learned it needs a browser must not be dragged back
    to tier 1 because someone asked for the phone."""
    from engine.core.fetch.escalation import starting_tier

    profile = DomainProfile("example.test", min_working_tier=Tier.STEALTH)
    assert starting_tier(profile, floor=Tier.IMPERSONATE) == Tier.STEALTH


# -- the language ------------------------------------------------------------


async def test_a_country_alone_sets_accept_language_at_tier_zero() -> None:
    """`location: {"country": "de"}` is the whole of what the API documents."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, text="<html><body><p>Ein Text.</p></body></html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = HttpFetcher(client=client)
    try:
        await fetcher.fetch(
            FetchRequest(url="https://example.com/", location=Location(country="de"))
        )
    finally:
        await client.aclose()

    assert "accept-language" in seen, "a country was given and no language was sent"
    assert seen["accept-language"].lower().startswith("de")


async def test_tier_zero_and_tier_one_derive_the_language_the_same_way() -> None:
    """One helper under both. They disagreed, and the cheap rung was wrong."""
    from engine.core.geo import accept_language_for

    location = Location(country="fr")
    expected = accept_language_for(location)
    assert expected, "the fixture country must map to a language"

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, text="<html><body><p>Du texte.</p></body></html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await HttpFetcher(client=client).fetch(
            FetchRequest(url="https://example.com/", location=location)
        )
    finally:
        await client.aclose()

    assert seen.get("accept-language") == expected


# -- the phone ---------------------------------------------------------------


def test_the_mobile_rung_impersonates_a_phone_not_a_desktop_in_a_hat() -> None:
    """A site reads the User-Agent AND the TLS fingerprint. Sending a phone
    UA from a desktop profile is a mismatch, which is itself a signal."""
    from engine.core.fetch.tier1_impersonate import ImpersonateFetcher

    fetcher = ImpersonateFetcher()
    assert fetcher.mobile_profile != fetcher.profile
    assert "android" in fetcher.mobile_profile or "ios" in fetcher.mobile_profile


def test_the_pinned_mobile_profile_is_one_curl_cffi_actually_has() -> None:
    """A profile name that does not exist fails at request time, on a real
    fetch, in production — never in a unit test that stubs the session."""
    curl_cffi = pytest.importorskip("curl_cffi.requests.impersonate")
    import typing

    from engine.settings import settings

    known = typing.get_args(curl_cffi.BrowserTypeLiteral)
    assert settings.impersonate_profile in known
    assert settings.impersonate_profile_mobile in known


def test_the_phone_fingerprint_does_not_contradict_itself() -> None:
    """`Sec-Ch-Ua-Mobile: ?0` beside a `Mobile Safari` User-Agent.

    curl_cffi's own chrome131_android profile ships that pair (measured
    against httpbin.org/headers, 9 Sep 2026); a real Android Chrome sends
    `?1`. These headers are ours to own precisely because a mismatch is a
    detection signal — and here the library is what mismatches.
    """
    import inspect

    from engine.core.fetch import tier1_impersonate

    source = inspect.getsource(tier1_impersonate.ImpersonateFetcher.fetch)
    assert 'headers["Sec-Ch-Ua-Mobile"] = "?1"' in source
    assert "req.mobile" in source
