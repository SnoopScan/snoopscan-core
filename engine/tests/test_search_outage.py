"""A search rung that answers 200 with nothing is broken, not empty.

SearXNG routes its upstream queries through our residential exit. When the
proxy password was rotated on 7 Sep 2026 its `settings.yml` kept the old one,
so every upstream 407'd and SearXNG answered **HTTP 200 with zero results** and
all six engines listed as unresponsive.

At the ladder that is indistinguishable from "there is no such thing", so it
fell through to DuckDuckGo — correct behaviour — and the three most
authoritative baby-name sources silently left every result set:

    thebump.com    18% of sources -> 0%
    nameberry.com  15%            -> 0%
    ancestry.com   13%            -> 0%

The run got FASTER, because it stopped fetching the hard, valuable pages. That
reads as an improvement unless you are looking for it.

SearXNG hands us the evidence in the same payload: `unresponsive_engines`.
Empty WITH failures is an outage; empty WITHOUT them is an honest empty web.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.core.search import SearchQuery, SearchUnavailable, SearXNGProvider


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def _client_returning(payload: dict[str, Any]) -> Any:
    class _Client:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, *a: Any, **k: Any) -> _Response:
            return _Response(payload)

    return _Client


@pytest.fixture
def provider() -> SearXNGProvider:
    return SearXNGProvider("http://127.0.0.1:8888")


async def _search(monkeypatch: Any, provider: SearXNGProvider, payload: dict[str, Any]):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _client_returning(payload)())
    return await provider.search(SearchQuery(query="aspen baby name", limit=8))


@pytest.mark.asyncio
async def test_empty_with_failed_engines_is_an_outage(
    monkeypatch: Any, provider: SearXNGProvider
) -> None:
    payload = {
        "results": [],
        "unresponsive_engines": [
            ["wikipedia", "HTTP connection error"],
            ["brave", "HTTP connection error"],
        ],
    }
    with pytest.raises(SearchUnavailable) as exc:
        await _search(monkeypatch, provider, payload)
    assert "wikipedia" in str(exc.value), "name the engines, so the cause is in the log"


@pytest.mark.asyncio
async def test_empty_with_healthy_engines_is_an_honest_empty_web(
    monkeypatch: Any, provider: SearXNGProvider
) -> None:
    """A query with genuinely no answers must NOT trip the ladder."""
    results = await _search(monkeypatch, provider, {"results": [], "unresponsive_engines": []})
    assert results == []


@pytest.mark.asyncio
async def test_results_with_one_flaky_engine_still_succeed(
    monkeypatch: Any, provider: SearXNGProvider
) -> None:
    """Partial degradation is normal — DuckDuckGo alone often CAPTCHAs."""
    payload = {
        "results": [{"url": "https://www.thebump.com/b/aspen-baby-name", "title": "Aspen"}],
        "unresponsive_engines": [["duckduckgo", "CAPTCHA"]],
    }
    results = await _search(monkeypatch, provider, payload)
    assert len(results) == 1 and "thebump" in results[0].url


def test_health_snapshot_names_tripped_rungs() -> None:
    from engine.core import search as search_mod

    search_mod._breakers.clear()
    search_mod._note_failure("searxng")
    snap = search_mod.health_snapshot()
    assert snap["searxng"]["consecutiveFailures"] == 1
    search_mod._breakers.clear()
