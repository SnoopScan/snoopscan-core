"""The ladder: one provider refusing us must not be the endpoint going down.

Measured on 4 September 2026, inside a single afternoon: DuckDuckGo went from
answering every query to refusing 24 of 24; Mojeek from answering to blocking
outright; Brave from 23/24 to HTTP 429. A single-provider search endpoint is
down three times in an afternoon. These pin the behaviour that stops that.
"""

from __future__ import annotations

import pytest

from engine.core import search as serp
from engine.core.search import SearchQuery, SearchResult, SearchUnavailable

pytestmark = pytest.mark.asyncio


class _Rung:
    def __init__(
        self,
        name: str,
        *,
        results: int = 0,
        raises: bool = False,
        supports: frozenset[str] | None = None,
    ) -> None:
        self.name = name
        self._results = results
        self._raises = raises
        self.calls = 0
        self.supports = (
            supports
            if supports is not None
            else frozenset(
                {
                    "country",
                    "place",
                    "language",
                    "device",
                    "freshness",
                    "safe_search",
                    "page",
                    "auto_correct",
                }
            )
        )

    async def search(self, q: SearchQuery) -> list[SearchResult]:
        self.calls += 1
        if self._raises:
            raise SearchUnavailable(f"{self.name} refused")
        return [SearchResult(url=f"https://{self.name}.test/{i}") for i in range(self._results)]


@pytest.fixture(autouse=True)
def _clean_breakers() -> None:
    serp._breakers.clear()


async def test_a_blocked_provider_falls_through_to_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dead, alive = _Rung("dead", raises=True), _Rung("alive", results=3)
    monkeypatch.setattr(serp, "ladder", lambda: [dead, alive])

    answer = await serp.search(SearchQuery(query="kettles", limit=10))

    assert answer.provider == "alive"
    assert len(answer.results) == 3
    assert dead.calls == 1, "the first rung is still tried once"


async def test_a_provider_that_parses_to_nothing_is_not_taken_as_an_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked engine usually returns a page we parse to zero results. Asking
    the next rung is cheap; telling an agent the web has nothing is not.
    """
    empty, full = _Rung("empty", results=0), _Rung("full", results=2)
    monkeypatch.setattr(serp, "ladder", lambda: [empty, full])

    answer = await serp.search(SearchQuery(query="kettles", limit=10))

    assert answer.provider == "full"
    assert len(answer.results) == 2


async def test_when_every_working_rung_finds_nothing_the_answer_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of that rule. An agent told "unavailable" retries; an agent
    told "no results" moves on. Only one of those is right when the web is empty.
    """
    monkeypatch.setattr(serp, "ladder", lambda: [_Rung("a"), _Rung("b")])

    answer = await serp.search(SearchQuery(query="a query with genuinely no hits", limit=10))

    assert answer.results == []


async def test_only_when_no_rung_works_is_search_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serp, "ladder", lambda: [_Rung("a", raises=True), _Rung("b", raises=True)])

    with pytest.raises(SearchUnavailable) as caught:
        await serp.search(SearchQuery(query="kettles", limit=10))

    assert "a" in str(caught.value) and "b" in str(caught.value)


async def test_a_repeatedly_failing_provider_stops_being_asked_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that just refused us will refuse the next request too, and the
    caller pays for that guess in latency. After the configured run of failures
    the rung is skipped until its backoff lapses.
    """
    monkeypatch.setattr(serp.settings, "search_breaker_failures", 2)
    monkeypatch.setattr(serp.settings, "search_breaker_seconds", 300)
    dead, alive = _Rung("dead", raises=True), _Rung("alive", results=1)
    monkeypatch.setattr(serp, "ladder", lambda: [dead, alive])

    for _ in range(3):
        await serp.search(SearchQuery(query="kettles", limit=10))

    assert dead.calls == 2, "tripped after two failures, then skipped"
    assert alive.calls == 3


async def test_a_success_closes_the_breaker_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recovery has to be automatic — nobody is watching at 3am."""
    monkeypatch.setattr(serp.settings, "search_breaker_failures", 2)
    flaky = _Rung("flaky", raises=True)
    monkeypatch.setattr(serp, "ladder", lambda: [flaky, _Rung("alive", results=1)])

    await serp.search(SearchQuery(query="q", limit=10))
    assert serp._breaker("flaky").failures == 1

    serp._note_success("flaky")
    assert serp._breaker("flaky").failures == 0
    assert serp._available("flaky")


async def test_a_pinned_provider_never_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator who names one vendor gets that vendor. Silently answering
    from somewhere else would make `search_provider` a lie.
    """
    monkeypatch.setattr(serp.settings, "search_provider", "duckduckgo")
    monkeypatch.setattr(serp.settings, "search_ladder", "searxng,duckduckgo")

    rungs = serp.ladder()

    assert [r.name for r in rungs] == ["duckduckgo"]


async def test_an_unconfigured_rung_is_left_out_rather_than_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SearXNG with no URL is not a rung. Including it would spend a request and
    a timeout on every search to learn what config already knows.
    """
    monkeypatch.setattr(serp.settings, "search_provider", "")
    monkeypatch.setattr(serp.settings, "searxng_url", "")
    monkeypatch.setattr(serp.settings, "search_ladder", "searxng,duckduckgo")

    assert [r.name for r in serp.ladder()] == ["duckduckgo"]

    monkeypatch.setattr(serp.settings, "searxng_url", "http://127.0.0.1:8888")
    assert [r.name for r in serp.ladder()] == ["searxng", "duckduckgo"]


async def test_the_canary_reports_a_thinning_ladder_before_it_is_an_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The warning that would have caught Mojeek going dark at lunchtime."""
    from engine.workers import scheduler

    monkeypatch.setattr(
        serp, "ladder", lambda: [_Rung("dead", raises=True), _Rung("alive", results=2)]
    )

    assert await scheduler.check_search_health() == 1


async def test_the_canary_counts_an_empty_rung_as_sick(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blocked engine usually answers 200 with nothing in it. That is not health."""
    from engine.workers import scheduler

    monkeypatch.setattr(serp, "ladder", lambda: [_Rung("empty", results=0)])

    assert await scheduler.check_search_health() == 0


# --- The bought rung ----------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, ValueError):
            raise self._payload
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.params: dict[str, str] = {}

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, url: str, params: dict[str, str]) -> _FakeResponse:
        self.params = params
        return self._response


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse) -> _FakeClient:
    import httpx

    client = _FakeClient(response)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    return client


async def test_scrapingdog_maps_the_documented_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """`organic_results[]` with `link`, `title`, `snippet` — their contract."""
    from engine.core.search import ScrapingdogProvider

    client = _patch_httpx(
        monkeypatch,
        _FakeResponse(
            200,
            {
                "organic_results": [
                    {
                        "title": "Football",
                        "link": "https://en.wikipedia.org/wiki/Football",
                        "snippet": "Football is a family of team sports...",
                    },
                    {"title": "No link here", "snippet": "dropped"},
                    {"title": "Second", "link": "https://example.com/2", "snippet": "s"},
                ]
            },
        ),
    )

    found = await ScrapingdogProvider("k").search(
        SearchQuery(query="football", limit=10, country="gb")
    )

    assert [r.url for r in found] == [
        "https://en.wikipedia.org/wiki/Football",
        "https://example.com/2",
    ]
    assert found[0].title == "Football"
    assert found[0].description.startswith("Football is a family")
    assert [r.position for r in found] == [1, 2], "position renumbers after a dropped row"
    assert client.params["country"] == "gb" and client.params["query"] == "football"


async def test_scrapingdog_honours_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.core.search import ScrapingdogProvider

    _patch_httpx(
        monkeypatch,
        _FakeResponse(
            200, {"organic_results": [{"link": f"https://example.com/{i}"} for i in range(20)]}
        ),
    )

    assert len(await ScrapingdogProvider("k").search(SearchQuery(query="q", limit=3))) == 3


async def test_a_shape_change_is_a_provider_failure_not_an_empty_web(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If they rename `organic_results`, the ladder must move on rather than
    tell the caller the web has nothing.
    """
    from engine.core.search import ScrapingdogProvider

    _patch_httpx(monkeypatch, _FakeResponse(200, {"results": [{"link": "https://x.test"}]}))

    with pytest.raises(SearchUnavailable):
        await ScrapingdogProvider("k").search(SearchQuery(query="q", limit=5))


async def test_a_keyless_scrapingdog_is_not_a_rung(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a key it would spend a request and a timeout on every search to
    learn what config already knows.
    """
    monkeypatch.setattr(serp.settings, "search_provider", "")
    monkeypatch.setattr(serp.settings, "searxng_url", "")
    monkeypatch.setattr(serp.settings, "scrapingdog_key", "")
    monkeypatch.setattr(serp.settings, "search_ladder", "searxng,scrapingdog,duckduckgo")

    assert [r.name for r in serp.ladder()] == ["duckduckgo"]

    monkeypatch.setattr(serp.settings, "scrapingdog_key", "sd_key")
    assert [r.name for r in serp.ladder()] == ["scrapingdog", "duckduckgo"]


# --- Parameters are requirements, not preferences -----------------------


async def test_a_rung_that_cannot_honour_the_ask_is_dropped_not_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mobile SERPs are a different page from desktop ones.

    A ladder that answers `device: mobile` from a rung with no device switch
    returns desktop results under a mobile label — wrong in a way the caller
    cannot see. The rung is dropped instead.
    """
    blind = _Rung("blind", results=5, supports=frozenset({"country"}))
    capable = _Rung("capable", results=3, supports=frozenset({"country", "device"}))
    monkeypatch.setattr(serp, "ladder", lambda: [blind, capable])

    answer = await serp.search(SearchQuery(query="shoes", limit=10, device="mobile"))

    assert answer.provider == "capable"
    assert blind.calls == 0, "the rung that could not honour it must not be asked"


async def test_an_unhonourable_ask_refuses_rather_than_answering_wrongly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        serp, "ladder", lambda: [_Rung("basic", results=5, supports=frozenset({"country"}))]
    )

    with pytest.raises(SearchUnavailable) as caught:
        await serp.search(SearchQuery(query="shoes", limit=10, device="mobile"))

    assert "device" in str(caught.value), "the refusal must name what could not be honoured"


async def test_an_unset_parameter_demands_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The common case stays cheap: a plain query must not exclude any rung."""
    basic = _Rung("basic", results=4, supports=frozenset())
    monkeypatch.setattr(serp, "ladder", lambda: [basic])

    answer = await serp.search(SearchQuery(query="shoes"))

    assert answer.provider == "basic"
    assert SearchQuery(query="shoes").demands() == frozenset()


async def test_defaults_are_not_demands() -> None:
    """`page=1` and `autoCorrect=True` are the defaults, not requests."""
    assert SearchQuery(query="q", page=1, auto_correct=True).demands() == frozenset()
    assert SearchQuery(query="q", page=2).demands() == {"page"}
    assert SearchQuery(query="q", auto_correct=False).demands() == {"auto_correct"}


async def test_searxng_declines_the_hour_window_it_cannot_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`supports` is per-parameter; SearXNG's time_range starts at a day, so
    "the last hour" is a value it cannot serve even though it does freshness.
    Widening the window silently would be the same bug in miniature.
    """
    from engine.core.search import SearXNGProvider

    sx = SearXNGProvider("http://127.0.0.1:8888")

    assert serp._can_honour(sx, SearchQuery(query="q", freshness="day"))
    assert not serp._can_honour(sx, SearchQuery(query="q", freshness="hour"))


async def test_the_bought_rung_is_the_one_that_can_answer_local_mobile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case that justifies paying: what a phone in Austin sees."""
    from engine.core.search import DuckDuckGoProvider, ScrapingdogProvider, SearXNGProvider

    q = SearchQuery(query="plumbers near me", place="Austin, Texas, United States", device="mobile")

    assert not serp._can_honour(DuckDuckGoProvider(), q)
    assert not serp._can_honour(SearXNGProvider("http://x"), q)
    assert serp._can_honour(ScrapingdogProvider("k"), q)


async def test_scrapingdog_sends_every_parameter_it_promised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A promise in `supports` that the request does not carry is a lie the
    ladder would act on."""
    from engine.core.search import ScrapingdogProvider

    client = _patch_httpx(monkeypatch, _FakeResponse(200, {"organic_results": []}))

    await ScrapingdogProvider("k").search(
        SearchQuery(
            query="plumbers",
            limit=10,
            country="GB",
            place="Austin, Texas, United States",
            language="en",
            device="mobile",
            freshness="week",
            safe_search="strict",
            page=3,
            auto_correct=False,
        )
    )

    sent = client.params
    assert sent["location"] == "Austin, Texas, United States"
    assert sent["device"] == "mobile"
    assert sent["tbs"] == "qdr:w"
    assert sent["safe"] == "active"
    assert sent["nfpr"] == "1"
    assert sent["language"] == "en"
    assert sent["country"] == "gb"
    assert sent["page"] == "2", "their pagination is 0-based; ours is 1-based"


async def test_scrapingdog_reports_googles_own_rank_not_a_row_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observed in the live response: rows carry `rank`, Google's real position.

    Counting rows would restart at 1 on page 2, quietly misreporting where a
    result sits — which is the one thing someone asking for page 2 wants to know.
    """
    from engine.core.search import ScrapingdogProvider

    _patch_httpx(
        monkeypatch,
        _FakeResponse(
            200,
            {
                "organic_results": [
                    {"link": "https://a.test", "rank": 11},
                    {"link": "https://b.test", "rank": 12},
                ]
            },
        ),
    )

    found = await ScrapingdogProvider("k").search(SearchQuery(query="q", limit=5, page=2))

    assert [r.position for r in found] == [11, 12]


async def test_related_searches_come_through_from_the_bought_rung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verified against a real response: {"query": ..., "link": ...} rows.

    They arrive free on the same call. Dropping them makes the caller pay for a
    second search to rediscover what the first one already knew.
    """
    from engine.core.search import ScrapingdogProvider

    _patch_httpx(
        monkeypatch,
        _FakeResponse(
            200,
            {
                "organic_results": [{"link": "https://a.test", "rank": 1}],
                "relatedSearches": [
                    {"query": "running shoes reddit", "link": "https://google.com/x"},
                    {"link": "https://google.com/y"},  # no query text: dropped
                ],
            },
        ),
    )
    provider = ScrapingdogProvider("k")

    await provider.search(SearchQuery(query="running shoes", limit=5))

    assert provider.last_related == ["running shoes reddit"]


async def test_the_response_carries_related_from_whichever_rung_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WithExtras(_Rung):
        last_related = ["widened query"]

    monkeypatch.setattr(serp, "ladder", lambda: [_WithExtras("rich", results=2)])

    answer = await serp.search(SearchQuery(query="q"))

    assert answer.related == ["widened query"]


# --- Billing follows the rung that answered -----------------------------


async def test_a_free_rung_bills_the_free_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Free(_Rung):
        paid = False

    monkeypatch.setattr(serp, "ladder", lambda: [_Free("searxng", results=2)])

    assert (await serp.search(SearchQuery(query="q"))).paid is False


async def test_a_bought_rung_marks_the_answer_paid(monkeypatch: pytest.MonkeyPatch) -> None:
    """The margin depends on this. A search the free rungs could not serve costs
    us real money per call, and billing both at the same rate is a loss that
    grows with traffic rather than shrinking with it.
    """

    class _Free(_Rung):
        paid = False

    class _Paid(_Rung):
        paid = True

    monkeypatch.setattr(
        serp, "ladder", lambda: [_Free("searxng", raises=True), _Paid("scrapingdog", results=2)]
    )

    answer = await serp.search(SearchQuery(query="q"))

    assert answer.provider == "scrapingdog"
    assert answer.paid is True


async def test_the_shipped_rungs_declare_their_side_of_the_bill() -> None:
    from engine.core.search import (
        DuckDuckGoProvider,
        ScrapingdogProvider,
        SearXNGProvider,
        SerperProvider,
    )

    assert SearXNGProvider("http://x").paid is False
    assert DuckDuckGoProvider().paid is False
    assert ScrapingdogProvider("k").paid is True
    assert SerperProvider("k").paid is True


async def test_the_paid_rate_is_dearer_than_the_free_one() -> None:
    from engine.core.credits import DEFAULT_COSTS

    assert DEFAULT_COSTS["search_paid"] > DEFAULT_COSTS["search"]


async def test_a_key_missing_from_the_operators_table_is_not_free() -> None:
    """The desk's table is a snapshot of the last time someone saved it.

    A billable key added since is absent from it, and absence must read as
    "not configured" rather than "free" — otherwise every new billable feature
    ships giving itself away until somebody reads the ledger.
    """
    from engine.core.credits import DEFAULT_COSTS, credits_for
    from engine.core.models import Cost

    stale_desk_table = {"direct": 1, "proxied": 2, "search": 2}  # saved before search_paid existed

    priced = credits_for(Cost(extras={"search_paid": 1}), stale_desk_table)

    assert priced == DEFAULT_COSTS["search_paid"], "a missing key fell through to free"


async def test_an_operator_can_still_deliberately_make_something_free() -> None:
    """Silence means default; zero means zero."""
    from engine.core.credits import credits_for
    from engine.core.models import Cost

    assert credits_for(Cost(extras={"search_paid": 1}), {"search_paid": 0}) == 0


async def test_a_free_rung_failing_onto_the_paid_one_is_flagged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The expensive kind of quiet failure.

    Nothing looks broken from outside — search still answers — but every query
    the free rung should have served is being bought at five times the credits.
    Losing the free rung is a cost incident, and it shows up as no outage
    anywhere, so it has to announce itself.
    """
    import logging

    class _Free(_Rung):
        paid = False

    class _Paid(_Rung):
        paid = True

    monkeypatch.setattr(
        serp, "ladder", lambda: [_Free("searxng", raises=True), _Paid("scrapingdog", results=2)]
    )

    with caplog.at_level(logging.WARNING):
        answer = await serp.search(SearchQuery(query="ordinary query"))

    assert answer.provider == "scrapingdog"
    assert "search_fell_back_to_paid" in caplog.text


async def test_asking_for_google_on_purpose_is_not_flagged_as_a_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A caller who asked for something only the bought rung can do is not a
    fallback — the free rungs were never eligible. Warning here would cry wolf
    on every mobile SERP and train everyone to ignore the real one.
    """
    import logging

    class _Free(_Rung):
        paid = False

    class _Paid(_Rung):
        paid = True

    monkeypatch.setattr(
        serp,
        "ladder",
        lambda: [
            _Free("searxng", results=5, supports=frozenset({"country"})),
            _Paid("scrapingdog", results=2),
        ],
    )

    with caplog.at_level(logging.WARNING):
        await serp.search(SearchQuery(query="plumbers", device="mobile"))

    assert "search_fell_back_to_paid" not in caplog.text


async def test_duckduckgo_retries_once_on_a_transport_miss() -> None:
    """A dropped connection is retried before it counts as a failure.

    On a one-rung ladder two misses trip the breaker and search is down for
    three minutes; the production box showed the first fetch returning no status
    and the next five succeeding.
    """
    from engine.core.fetch.base import FetchResult
    from engine.core.search import DuckDuckGoProvider, SearchQuery

    page = (
        '<table><tr><td><a class="result-link" '
        'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">A</a></td></tr></table>'
    )

    class Flaky:
        calls = 0

        async def fetch(self, req: object) -> FetchResult:
            Flaky.calls += 1
            if Flaky.calls == 1:
                return FetchResult(
                    url="u",
                    status_code=None,
                    headers={},
                    body=b"",
                    content_type=None,
                    tier="impersonate",
                    latency_ms=1,
                    bytes_transferred=0,
                    error="dropped",
                )
            return FetchResult(
                url="u",
                status_code=200,
                headers={},
                body=page.encode(),
                content_type="text/html",
                tier="impersonate",
                latency_ms=1,
                bytes_transferred=len(page),
            )

    found = await DuckDuckGoProvider(fetcher=Flaky()).search(SearchQuery(query="x", limit=5))
    assert Flaky.calls == 2
    assert [r.url for r in found] == ["https://example.com/a"]
