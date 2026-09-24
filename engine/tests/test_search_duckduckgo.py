"""The engine searches for itself.

`lite.duckduckgo.com` publishes `Allow: /` for every user agent and answers the
impersonation tier without a proxy, so `/v1/search` works with no vendor and no
key. The fixture is a real response, trimmed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from engine.core.fetch.base import FetchResult
from engine.core.models import Tier
from engine.core.search import (
    DuckDuckGoProvider,
    NullProvider,
    SearchQuery,
    SearchUnavailable,
    SerperProvider,
    get_provider,
)
from engine.settings import settings

FIXTURE = (Path(__file__).parent / "fixtures" / "ddg_lite.html").read_text()


class StubFetcher:
    """Answers with the fixture, or with whatever status the test asks for."""

    def __init__(self, html: str = FIXTURE, status: int = 200) -> None:
        self.html, self.status, self.requests = html, status, []

    async def fetch(self, req: Any) -> FetchResult:
        self.requests.append(req)
        body = self.html.encode()
        return FetchResult(
            url=req.url,
            status_code=self.status,
            headers={},
            body=body,
            content_type="text/html; charset=utf-8",
            tier=str(Tier.IMPERSONATE),
            latency_ms=12,
            bytes_transferred=len(body),
        )


def test_results_carry_the_destination_not_the_redirect() -> None:
    results = DuckDuckGoProvider.parse(FIXTURE)

    assert len(results) == 3
    assert all(r.url.startswith("http") for r in results)
    assert not any("duckduckgo.com" in r.url for r in results)
    assert all(r.title for r in results)
    assert all(r.description for r in results)
    assert [r.position for r in results] == [1, 2, 3]


def test_a_link_that_is_not_a_result_is_dropped() -> None:
    assert DuckDuckGoProvider.unwrap("//duckduckgo.com/settings") is None
    assert DuckDuckGoProvider.unwrap("/lite/?q=next") is None
    assert DuckDuckGoProvider.unwrap("https://example.com/page") == "https://example.com/page"
    assert (
        DuckDuckGoProvider.unwrap("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.test%2Fx&rut=9")
        == "https://a.test/x"
    )


def test_the_query_and_the_region_reach_duckduckgo() -> None:
    fetcher = StubFetcher()
    results = asyncio.run(
        DuckDuckGoProvider(fetcher).search(SearchQuery(query="jaffa cake", limit=2, country="GB"))
    )

    assert len(results) == 2, "the limit is respected"
    url = fetcher.requests[0].url
    assert url.startswith("https://lite.duckduckgo.com/lite/?")
    assert "q=jaffa+cake" in url and "kl=uk-en" in url

    asyncio.run(DuckDuckGoProvider(fetcher).search(SearchQuery(query="x", limit=1, country="zz")))
    assert "kl=wt-wt" in fetcher.requests[1].url, "an unknown country searches without a region"


def test_an_outage_is_not_an_empty_web() -> None:
    with pytest.raises(SearchUnavailable):
        asyncio.run(
            DuckDuckGoProvider(StubFetcher(status=503)).search(SearchQuery(query="x", limit=5))
        )

    class Broken:
        async def fetch(self, req: Any) -> FetchResult:
            raise ConnectionError("down")

    with pytest.raises(SearchUnavailable):
        asyncio.run(DuckDuckGoProvider(Broken()).search(SearchQuery(query="x", limit=5)))


def test_search_works_with_nothing_configured_and_a_vendor_still_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "search_provider", "")
    monkeypatch.setattr(settings, "search_api_key", "")
    assert isinstance(get_provider(), DuckDuckGoProvider)

    monkeypatch.setattr(settings, "search_provider", "serper")
    monkeypatch.setattr(settings, "search_api_key", "key")
    assert isinstance(get_provider(), SerperProvider)

    # A deployment that wants no search at all still gets the honest refusal.
    monkeypatch.setattr(settings, "search_provider", "none")
    assert isinstance(get_provider(), NullProvider)
