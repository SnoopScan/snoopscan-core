"""In production, a fetch may not leave from this host's own address.

The address customers connect to and the address targets see must not be the
same one. When they are: one abusive customer puts it on a WAF reputation list
and tiers 0 and 1 stop working for everybody; abuse reports arrive at the host
running the API; and any caller can read the address straight back by scraping
an echo service.

Firecrawl publishes exactly this split in its own docs — no fixed set of
outbound IPs, identity asserted by `FirecrawlAgent` in the User-Agent, and one
static address published for WEBHOOKS only, which customers allowlist inbound.
Egress and callback are separate addresses with opposite policies.

Enforced where a request is BUILT, not where it is sent. Thirty-odd places
construct a FetchRequest — platform shortcuts, robots, sitemaps, search
providers, map, the lead pipeline — and only the scrape path consults the proxy
selector. A per-caller check would be a policy with thirty holes in it.
"""

from __future__ import annotations

import pytest

from engine.core.fetch.base import DirectEgressRefused, FetchRequest


@pytest.fixture
def no_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.settings import settings

    monkeypatch.setattr(settings, "allow_direct_egress", False)


def test_a_direct_fetch_is_refused_when_the_deployment_forbids_it(no_direct: None) -> None:
    with pytest.raises(DirectEgressRefused):
        FetchRequest(url="https://example.com/page")


def test_a_proxied_fetch_is_always_allowed(no_direct: None) -> None:
    req = FetchRequest(url="https://example.com/page", proxy_url="http://user:pw@exit:8000")
    assert req.proxy_url


def test_direct_is_allowed_by_default_so_a_laptop_still_works() -> None:
    assert FetchRequest(url="https://example.com/page").proxy_url is None


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8888/search",  # SearXNG
        "http://localhost:8099/ready",
        "http://[::1]:8888/search",
        "http://10.0.0.5/internal",
        "http://192.168.1.10/internal",
    ],
)
def test_our_own_services_are_not_egress(no_direct: None, url: str) -> None:
    """SearXNG on loopback must never be dialled through a residential exit,
    and nothing outside the host can see it. Refusing these would take the
    search ladder down the moment the policy was switched on."""
    assert FetchRequest(url=url).proxy_url is None


@pytest.mark.parametrize(
    "url",
    [
        "https://api.github.com/orgs/x/repos",
        "https://example.com/robots.txt",
        "https://example.com/sitemap.xml",
        "https://shop.example.com/products.json",
    ],
)
def test_every_kind_of_public_fetch_is_covered(no_direct: None, url: str) -> None:
    """Robots, sitemaps, platform JSON and API calls all leave the building the
    same way a page fetch does. They bypass the proxy selector entirely, which
    is exactly why the check lives at construction."""
    with pytest.raises(DirectEgressRefused):
        FetchRequest(url=url)


def test_the_refusal_names_the_host_and_the_setting(no_direct: None) -> None:
    """An operator reading this in a log needs to know what was refused and
    which switch caused it."""
    with pytest.raises(DirectEgressRefused) as excinfo:
        FetchRequest(url="https://target.example/page")
    message = str(excinfo.value)
    assert "target.example" in message
    assert "allow_direct_egress" in message


# --------------------------------------------------------------------------
# A deployment that can serve nothing must not start
# --------------------------------------------------------------------------


def test_forbidding_direct_egress_without_a_proxy_refuses_to_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise the service is up, healthy, and answers every scrape with a
    503 that reads like the target's fault."""
    from engine.settings import settings

    monkeypatch.setattr(settings, "allow_direct_egress", False)
    monkeypatch.setattr(settings, "proxy_enabled", False)
    with pytest.raises(RuntimeError, match="allow_direct_egress"):
        settings.assert_egress_is_servable()


@pytest.mark.parametrize(
    "direct,proxy",
    [(True, False), (True, True), (False, True)],
)
def test_every_servable_combination_boots(
    monkeypatch: pytest.MonkeyPatch, direct: bool, proxy: bool
) -> None:
    from engine.settings import settings

    monkeypatch.setattr(settings, "allow_direct_egress", direct)
    monkeypatch.setattr(settings, "proxy_enabled", proxy)
    settings.assert_egress_is_servable()
