"""POST /v1/places/search — the public contract over a proprietary source.

Two facts to pin: the open core answers 503 with a reason rather than 500 when
the module is absent, and a successful call is billed in the units the caller
can predict — one `places_search` per page, one `places_detail` per panel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from engine.api import billing
from engine.api.routes import places as places_routes
from engine.core.errors import ErrorCode, InvalidRequest, PlacesUnavailable
from engine.places.models import Place, PlacesSearchData


@dataclass
class FakeKey:
    id: str = "key_test"
    rate_limit_rpm: int = 60
    allow_js_exec: bool = False


class FakePlaces:
    def __init__(self, detail_fetches: int) -> None:
        self.detail_fetches = detail_fetches
        self.requests: list[Any] = []

    async def search(self, req: Any) -> PlacesSearchData:
        self.requests.append(req)
        return PlacesSearchData(
            query=req.query,
            location=req.location,
            places=[
                Place(
                    feature_id="0x1:0x2",
                    name="Jo's Coffee",
                    place_url="https://www.google.com/maps/place/Jo",
                    website="https://www.joscoffee.example" if req.includeDetails else None,
                )
            ],
            search_pages=1,
            detail_fetches=self.detail_fetches,
            enriched=0,
        )


async def test_the_open_core_answers_503_with_a_reason(monkeypatch: Any) -> None:
    def absent(_service: Any) -> Any:
        raise PlacesUnavailable()

    monkeypatch.setattr(places_routes, "_load_service", absent)
    with pytest.raises(PlacesUnavailable) as err:
        await places_routes.places_search({"query": "coffee"}, FakeKey(), object())
    assert err.value.code == ErrorCode.PLACES_UNAVAILABLE
    assert err.value.http_status == 503
    assert err.value.detail == {"reason": "places_unavailable"}


async def test_a_search_is_billed_per_page_and_per_detail_panel(monkeypatch: Any) -> None:
    fake = FakePlaces(detail_fetches=2)
    charged: list[dict[str, Any]] = []

    monkeypatch.setattr(places_routes, "_load_service", lambda _s: fake)
    monkeypatch.setattr(billing, "assert_credits", lambda key: None)

    async def fake_charge(key: Any, *, endpoint: str, url: str | None, cost: Any) -> None:
        charged.append({"endpoint": endpoint, "url": url, "extras": dict(cost.extras)})

    monkeypatch.setattr(billing, "charge", fake_charge)

    body = {"query": "coffee shops", "location": "Austin, TX", "includeDetails": True, "limit": 5}
    out = await places_routes.places_search(body, FakeKey(), object())

    assert out["success"] is True
    assert out["data"]["places"][0]["name"] == "Jo's Coffee"
    assert out["data"]["detail_fetches"] == 2
    assert out["data"]["cost"]["extras"] == {"places_search": 1, "places_detail": 2}
    assert charged == [
        {
            "endpoint": "places",
            "url": "https://www.google.com/maps/search/coffee+shops+in+Austin%2C+TX",
            "extras": {"places_search": 1, "places_detail": 2},
        }
    ]
    assert fake.requests[0].limit == 5 and fake.requests[0].includeDetails is True


async def test_an_unknown_field_is_a_400_that_names_it(monkeypatch: Any) -> None:
    """The body is a dict, so FastAPI never validates it; a pydantic error
    raised inside the handler used to fall through to the 500 handler."""
    monkeypatch.setattr(places_routes, "_load_service", lambda _s: FakePlaces(0))
    with pytest.raises(InvalidRequest) as err:
        await places_routes.places_search({"query": "coffee", "radius": 5}, FakeKey(), object())
    assert err.value.http_status == 400
    assert [p["field"] for p in err.value.detail["problems"]] == ["radius"]


@pytest.mark.parametrize(
    "smuggled",
    [
        {"actions": [{"type": "executeJavascript", "script": "fetch('https://x.test')"}]},
        {"scrapeOptions": {"actions": [{"type": "executeJavascript", "script": "1"}]}},
    ],
)
async def test_a_script_cannot_ride_in_on_a_places_request(
    monkeypatch: Any, smuggled: dict[str, Any]
) -> None:
    """This route has no scrape options, so the executeJavascript gate does not
    run on it; `extra="forbid"` is what keeps a script out. Pinned here because
    test_js_gate.py skips routes whose body cannot carry an action."""
    fake = FakePlaces(0)
    monkeypatch.setattr(places_routes, "_load_service", lambda _s: fake)
    with pytest.raises(InvalidRequest):
        await places_routes.places_search({"query": "coffee", **smuggled}, FakeKey(), object())
    assert fake.requests == [], "the search ran on a request that failed validation"
