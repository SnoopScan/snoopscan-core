"""POST /v1/serp — Google results pages with the AI Overview, per country.

Google's own results page cannot be fetched dependably — plain HTTP gets a
JavaScript wall and every browser rung gets the unusual-traffic page, through
residential exits in the country asked for (measured Sep 2026) — so the page
is bought. These pin the contract with the provider (the request we send, the
response shape we read, taken from its documentation) and the monitoring
answer on top: where a domain ranks, and whether the AI Overview cites it.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from engine.api import billing
from engine.api.routes import serp as serp_routes
from engine.core import serp
from engine.core.credits import credits_for
from engine.core.errors import ErrorCode, InvalidRequest, SerpUnavailable
from engine.core.models import Cost


def _payload(items: list[dict[str, Any]], status: int = 20000) -> dict[str, Any]:
    return {
        "status_code": 20000,
        "tasks": [
            {
                "status_code": status,
                "status_message": "Ok." if status == 20000 else "Invalid Field: 'location_code'.",
                "result": [
                    {
                        "keyword": "best running shoes",
                        "check_url": "https://www.google.com/search?q=best+running+shoes",
                        "item_types": sorted({i["type"] for i in items}),
                        "items": items,
                    }
                ],
            }
        ],
    }


# Shapes as the provider documents them: organic results, and an ai_overview
# carrying references both on the item and inside its elements.
ITEMS: list[dict[str, Any]] = [
    {
        "type": "ai_overview",
        "rank_group": 1,
        "rank_absolute": 1,
        "asynchronous_ai_overview": True,
        "markdown": "The best running shoes for most people are cushioned trainers.",
        "items": [
            {
                "type": "ai_overview_element",
                "text": "Cushioned trainers suit most runners.",
                "references": [
                    {
                        "type": "ai_overview_reference",
                        "source": "Runner's World",
                        "domain": "www.runnersworld.com",
                        "url": "https://www.runnersworld.com/gear/best-shoes",
                        "title": "Best running shoes",
                    }
                ],
            }
        ],
        "references": [
            {
                "type": "ai_overview_reference",
                "source": "Allbirds",
                "domain": "www.allbirds.com",
                "url": "https://www.allbirds.com/pages/running",
                "title": "Running shoes",
            },
            {
                "type": "ai_overview_reference",
                "source": "Runner's World",
                "domain": "www.runnersworld.com",
                "url": "https://www.runnersworld.com/gear/best-shoes",
                "title": "Best running shoes",
            },
        ],
    },
    {
        "type": "organic",
        "rank_group": 1,
        "rank_absolute": 2,
        "domain": "www.runnersworld.com",
        "url": "https://www.runnersworld.com/gear/best-shoes",
        "title": "Best running shoes 2026",
        "description": "Tested by our editors.",
    },
    {"type": "people_also_ask", "rank_group": 1, "rank_absolute": 3},
    {
        "type": "organic",
        "rank_group": 2,
        "rank_absolute": 4,
        "domain": "shop.allbirds.com",
        "url": "https://shop.allbirds.com/collections/running",
        "title": "Allbirds running",
        "description": "Tree Dasher.",
    },
]


def _req(**kw: Any) -> serp.SerpRequest:
    return serp.SerpRequest.model_validate({"keyword": "best running shoes", **kw})


# ------------------------------------------------------------------ request


def test_the_task_carries_googles_location_id_for_the_country() -> None:
    (task,) = serp.task_body(_req(country="GB", device="mobile", language="en"))
    assert task["location_code"] == 2826, "United Kingdom: 2000 + ISO numeric 826"
    assert task["device"] == "mobile" and task["language_code"] == "en"
    assert task["keyword"] == "best running shoes"


def test_the_us_code_matches_the_providers_own_example() -> None:
    assert serp.location_code("us") == 2840


def test_the_ai_overview_is_asked_for_only_when_wanted() -> None:
    assert serp.task_body(_req())[0]["load_async_ai_overview"] is True
    assert "load_async_ai_overview" not in serp.task_body(_req(aiOverview=False))[0]


def test_an_unknown_country_names_the_way_out() -> None:
    with pytest.raises(InvalidRequest) as err:
        serp.location_code("zz")
    assert "locationCode" in str(err.value)


def test_an_explicit_location_code_wins() -> None:
    assert serp.task_body(_req(country="zz", locationCode=1006886))[0]["location_code"] == 1006886


def test_a_domain_given_as_a_url_is_reduced_to_the_host() -> None:
    assert _req(domain="https://www.Allbirds.com/pages/x").domain == "www.allbirds.com"


# ----------------------------------------------------------------- response


def test_organic_results_keep_their_page_position() -> None:
    out = serp.parse(_payload(ITEMS), _req())
    assert [(o["position"], o["domain"]) for o in out["organic"]] == [
        (2, "www.runnersworld.com"),
        (4, "shop.allbirds.com"),
    ]


def test_the_ai_overview_lists_every_cited_page_once() -> None:
    out = serp.parse(_payload(ITEMS), _req())
    overview = out["aiOverview"]
    assert overview["present"] is True
    assert "cushioned trainers" in overview["text"]
    assert [r["url"] for r in overview["references"]] == [
        "https://www.allbirds.com/pages/running",
        "https://www.runnersworld.com/gear/best-shoes",
    ]


def test_the_monitored_domain_is_ranked_and_its_citation_found() -> None:
    """Subdomains are the same site: shop.allbirds.com ranks for allbirds.com."""
    out = serp.parse(_payload(ITEMS), _req(domain="allbirds.com"))
    assert out["domain"]["position"] == 4
    assert out["domain"]["url"] == "https://shop.allbirds.com/collections/running"
    assert out["domain"]["citedInAiOverview"] is True


def test_a_domain_that_is_absent_says_so_plainly() -> None:
    out = serp.parse(_payload(ITEMS), _req(domain="example.org"))
    assert out["domain"] == {
        "domain": "example.org",
        "position": None,
        "url": None,
        "citedInAiOverview": False,
        "citations": [],
    }


def test_no_ai_overview_is_reported_as_absent_not_empty() -> None:
    out = serp.parse(_payload([i for i in ITEMS if i["type"] != "ai_overview"]), _req())
    assert out["aiOverview"] == {"present": False}


def test_a_refused_task_is_a_503_not_an_empty_page() -> None:
    with pytest.raises(SerpUnavailable) as err:
        serp.parse(_payload([], status=40501), _req())
    assert "location_code" in str(err.value)


# ------------------------------------------------------------------- wire


async def test_it_sends_basic_auth_and_the_task_array(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(serp.settings, "dataforseo_login", "api-login")
    monkeypatch.setattr(serp.settings, "dataforseo_password", "api-pass")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_payload(ITEMS))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await serp.fetch(_req(country="de"), client=client)

    assert seen["url"] == serp.ENDPOINT
    assert seen["auth"] == "Basic " + base64.b64encode(b"api-login:api-pass").decode()
    assert seen["body"][0]["location_code"] == 2276
    assert out["organic"]


async def test_without_credentials_it_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(serp.settings, "dataforseo_login", "")
    monkeypatch.setattr(serp.settings, "dataforseo_password", "")
    with pytest.raises(SerpUnavailable) as err:
        await serp.fetch(_req())
    assert err.value.code == ErrorCode.SERP_UNAVAILABLE


async def test_rejected_credentials_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(serp.settings, "dataforseo_login", "x")
    monkeypatch.setattr(serp.settings, "dataforseo_password", "y")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={}))
    ) as client:
        with pytest.raises(SerpUnavailable) as err:
            await serp.fetch(_req(), client=client)
    assert "credentials" in str(err.value)


# ------------------------------------------------------------------ billing


@dataclass
class FakeKey:
    id: str = "key_test"
    rate_limit_rpm: int = 60
    allow_js_exec: bool = False


async def _route(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> list[dict[str, Any]]:
    charged: list[dict[str, Any]] = []
    monkeypatch.setattr(billing, "assert_credits", lambda key: None)

    async def fake_charge(key: Any, *, endpoint: str, url: str | None, cost: Any) -> None:
        charged.append({"endpoint": endpoint, "extras": dict(cost.extras)})

    async def fake_fetch(req: Any, client: Any = None) -> dict[str, Any]:
        return serp.parse(_payload(ITEMS), req)

    monkeypatch.setattr(billing, "charge", fake_charge)
    monkeypatch.setattr(serp, "fetch", fake_fetch)
    await serp_routes.serp_search(_req(**kw), FakeKey())
    return charged


async def test_a_call_is_billed_and_the_overview_on_top(monkeypatch: pytest.MonkeyPatch) -> None:
    charged = await _route(monkeypatch)
    assert charged == [{"endpoint": "serp", "extras": {"serp": 1, "serp_ai_overview": 1}}]
    assert credits_for(Cost(extras=charged[0]["extras"])) == 20


async def test_no_overview_no_overview_charge(monkeypatch: pytest.MonkeyPatch) -> None:
    charged = await _route(monkeypatch, aiOverview=False)
    assert charged[0]["extras"] == {"serp": 1}
    assert credits_for(Cost(extras={"serp": 1})) == 10


async def test_an_unavailable_provider_charges_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    charged: list[Any] = []
    monkeypatch.setattr(billing, "assert_credits", lambda key: None)

    async def refuse(req: Any, client: Any = None) -> dict[str, Any]:
        raise SerpUnavailable("not configured")

    async def fake_charge(*a: Any, **k: Any) -> None:
        charged.append(1)

    monkeypatch.setattr(serp, "fetch", refuse)
    monkeypatch.setattr(billing, "charge", fake_charge)
    with pytest.raises(SerpUnavailable):
        await serp_routes.serp_search(_req(), FakeKey())
    assert charged == []


async def test_a_bad_country_is_a_400_even_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's mistake is reported first, not hidden behind a 503."""
    monkeypatch.setattr(serp.settings, "dataforseo_login", "")
    monkeypatch.setattr(serp.settings, "dataforseo_password", "")
    with pytest.raises(InvalidRequest):
        await serp.fetch(_req(country="zz"))
