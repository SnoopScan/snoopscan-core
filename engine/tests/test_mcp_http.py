"""The hosted MCP endpoint (08-mcp-server.md section 7, Auth).

A bearer key from the same table as REST, refused in the REST envelope when
missing; the tools an agent gets; a scrape metered to the caller's key; an
empty balance refused before anything is fetched; guardrails kept per session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from engine.api import billing, deps
from engine.api.app import app
from engine.core.credits import OPERATOR_OWNER
from engine.core.fetch.base import FetchRequest, FetchResult
from engine.core.models import Cost, Tier
from engine.core.scrape_service import ScrapeService
from engine.mcp import http as mcp_http
from engine.settings import settings
from engine.storage.repositories import ApiKey

HTML = (
    "<html><head><title>Kettle guide</title></head><body><article><h1>Kettle guide</h1>"
    "<p>Steel kettles last longer than plastic ones, and this paragraph is here so the "
    "extractor has a body to keep. It goes on a little so the word count is honest.</p>"
    "</article></body></html>"
)


class StubFetcher:
    name = "http"

    async def fetch(self, req: FetchRequest) -> FetchResult:
        body = HTML.encode()
        return FetchResult(
            url=req.url,
            status_code=200,
            headers={},
            body=body,
            content_type="text/html; charset=utf-8",
            tier=self.name,
            latency_ms=42,
            bytes_transferred=len(body),
        )

    async def healthcheck(self) -> bool:
        return True


def _key(credits: int = 10, owner: str | None = "own_1") -> ApiKey:
    return ApiKey(
        id="key_mcp",
        label="customer",
        scopes=["scrape", "crawl", "map"],
        rate_limit_rpm=1000,
        allow_js_exec=False,
        webhook_secret=None,
        active=True,
        owner_ref=owner,
        credits_remaining=credits,
    )


@pytest.fixture
def charges(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    # The test hosts do not resolve; the SSRF guard is the REST suite's concern.
    monkeypatch.setattr(settings, "ssrf_guard_enabled", False)
    seen: list[dict[str, Any]] = []

    async def fake_charge(
        key: ApiKey, *, endpoint: str, url: str | None, cost: Cost, job_id: str | None = None
    ) -> int:
        # Mirrors the real charge(): EVERY key is metered, operator keys too.
        seen.append({"key": key.id, "endpoint": endpoint, "url": url, "tier": cost.tier})
        return key.credits_remaining - 1

    monkeypatch.setattr(billing, "charge", fake_charge)
    monkeypatch.setattr(
        deps, "get_service", lambda: ScrapeService({Tier.HTTP: StubFetcher()}, persist=False)
    )
    return seen


def _install_key(monkeypatch: pytest.MonkeyPatch, key: ApiKey | None) -> None:
    async def fake_require(request: Any, authorization: str | None = None) -> ApiKey:
        from engine.core.errors import Unauthorized

        if key is None or not (authorization or "").startswith("Bearer sk_"):
            raise Unauthorized()
        return key

    monkeypatch.setattr(deps, "require_api_key", fake_require)


@asynccontextmanager
async def connected(
    token: str | None = "sk_live_test", path: str = "/mcp"
) -> AsyncIterator[Client]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with mcp_http.lifespan():
        http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://engine.test", headers=headers
        )
        async with Client(
            streamable_http_client(f"http://engine.test{path}", http_client=http)
        ) as client:
            yield client


@pytest.mark.anyio
async def test_a_keyless_client_connects_and_is_told_how_to_get_a_key(
    monkeypatch: pytest.MonkeyPatch, charges: list[dict[str, Any]]
) -> None:
    """No key: connect and list tools, and every call answers with setup steps.

    A bare 401 left Claude Code with "failed to connect" and the agent with
    nothing to tell the person but "you need a key", so it invented a
    placeholder. Nothing may be fetched or charged: the tools read "no key"
    as the trusted local transport, so a keyless call must never reach them.
    """
    _install_key(monkeypatch, None)
    async with connected(token=None) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert "scrape" in tools
        result = await client.call_tool("scrape", {"url": "https://example.com/p"})
    text = result.content[0].text  # type: ignore[union-attr]
    assert result.is_error is True
    assert "snoopscan login" in text
    assert "/register" in text and "/app/keys" in text
    assert "claude mcp add --transport http snoopscan https://engine.test/mcp" in text
    assert "without an API key" in text
    assert charges == [], "a keyless call was charged"


@pytest.mark.anyio
async def test_a_wrong_key_is_told_it_is_not_recognised(
    monkeypatch: pytest.MonkeyPatch, charges: list[dict[str, Any]]
) -> None:
    _install_key(monkeypatch, None)
    async with connected(token="not-a-key") as client:
        result = await client.call_tool("scrape", {"url": "https://example.com/p"})
    assert result.is_error is True
    assert "not recognised" in result.content[0].text  # type: ignore[union-attr]
    assert charges == []


@pytest.mark.anyio
async def test_the_sign_in_address_asks_an_app_to_sign_in(
    monkeypatch: pytest.MonkeyPatch, charges: list[dict[str, Any]]
) -> None:
    """/mcp-oauth: no key is a 401 that points at the sign-in, the MCP way.

    The Claude app's connector screen takes a URL and nothing else. It only
    opens a browser sign-in when the server answers 401 with where to sign in;
    the keyless listing /mcp gives would leave it connected but useless.
    """
    _install_key(monkeypatch, None)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://engine.test"
    ) as http:
        init = {"jsonrpc": "2.0", "id": 1, "params": {}, "method": "initialize"}
        r = await http.post(
            "/mcp-oauth", json=init, headers={"Accept": "application/json, text/event-stream"}
        )
        assert r.status_code == 401
        challenge = r.headers["www-authenticate"]
        where = "https://engine.test/.well-known/oauth-protected-resource/mcp-oauth"
        assert f'resource_metadata="{where}"' in challenge

        for path in (
            "/.well-known/oauth-protected-resource/mcp-oauth",
            "/.well-known/oauth-protected-resource",
        ):
            meta = (await http.get(path)).json()
            assert meta["resource"] == "https://engine.test/mcp-oauth"
            assert meta["authorization_servers"] == [settings.account_url.rstrip("/")]
    assert charges == []


@pytest.mark.anyio
async def test_a_signed_in_app_uses_the_tools_like_any_key(
    monkeypatch: pytest.MonkeyPatch, charges: list[dict[str, Any]]
) -> None:
    """The token an app gets from signing in is an API key, metered like one."""
    _install_key(monkeypatch, _key())
    async with connected(path="/mcp-oauth") as client:
        result = await client.call_tool("scrape", {"url": "https://example.com/p"})
    assert result.is_error is False
    assert [c["key"] for c in charges] == ["key_mcp"]


@pytest.mark.anyio
async def test_rest_says_how_to_get_a_key_too() -> None:
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://engine.test"
    ) as http:
        r = await http.post("/v1/scrape", json={"url": "https://example.com"})
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "UNAUTHORIZED"
    assert "snoopscan login" in err["message"]
    assert err["detail"]["signupUrl"].endswith("/register")
    assert err["detail"]["keysUrl"].endswith("/app/keys")


@pytest.mark.anyio
async def test_the_tools_an_agent_gets(
    monkeypatch: pytest.MonkeyPatch, charges: list[dict[str, Any]]
) -> None:
    _install_key(monkeypatch, _key())
    async with connected() as client:
        tools = {t.name for t in (await client.list_tools()).tools}
    assert {
        "scrape",
        "search",
        "map",
        "crawl",
        "crawlStatus",
        "crawlPages",
        "extract",
        "checkChanges",
        "domain",
        "company",
        "people",
        "hiring",
        "findLeads",
        "leadsStatus",
    } <= tools
    assert "executeJavascript" not in tools


@pytest.mark.anyio
async def test_a_scrape_is_metered_to_the_calling_key(
    monkeypatch: pytest.MonkeyPatch, charges: list[dict[str, Any]]
) -> None:
    _install_key(monkeypatch, _key())
    async with connected() as client:
        result = await client.call_tool("scrape", {"url": "https://shop.test/kettles"})
    text = result.content[0].text
    assert "Kettle guide" in text
    assert charges == [
        {"key": "key_mcp", "endpoint": "scrape", "url": "https://shop.test/kettles", "tier": "http"}
    ]


@pytest.mark.anyio
async def test_an_empty_balance_is_refused_before_anything_is_fetched(
    monkeypatch: pytest.MonkeyPatch, charges: list[dict[str, Any]]
) -> None:
    _install_key(monkeypatch, _key(credits=0))
    async with connected() as client:
        result = await client.call_tool("scrape", {"url": "https://shop.test/kettles"})
    text = result.content[0].text.lower()
    assert "credit" in text
    assert charges == []


@pytest.mark.anyio
async def test_an_operator_key_is_metered_but_never_refused(
    monkeypatch: pytest.MonkeyPatch, charges: list[dict[str, Any]]
) -> None:
    """Our own work is billed like anyone's, and never stopped for want of credit.

    This asserted the opposite until 16 Sep 2026: an operator key was exempt
    from metering entirely, so MCP sessions fetched — real proxy bandwidth,
    really paid for — and left no usage row anywhere. A zero balance here is
    deliberate: it must still run, and it must still record.
    """
    _install_key(monkeypatch, _key(credits=0, owner=OPERATOR_OWNER))
    async with connected() as client:
        result = await client.call_tool("scrape", {"url": "https://shop.test/kettles"})
    assert "Kettle guide" in result.content[0].text
    assert [c["endpoint"] for c in charges] == ["scrape"]


def test_budgets_are_per_session_and_forgotten_when_idle() -> None:
    budgets = mcp_http.SessionBudgets(idle_seconds=0, max_sessions=2)
    a = budgets.get("session-a")
    a.record_pages(3)
    assert budgets.get("session-a").pages_fetched == 3
    assert budgets.get("session-b").pages_fetched == 0
    # A third session over the cap prunes the idle ones
    # (idle_seconds=0 makes every earlier one idle).
    budgets.get("session-c")
    assert len(budgets) <= 2


def test_an_empty_account_tells_the_agent_to_ask_the_person_not_retry() -> None:
    """Out of credits used to read "Try a different URL or source", so an agent
    went round other URLs while the account sat at zero."""
    from engine.core.errors import InsufficientCredits
    from engine.mcp.errors import explain

    text = explain(InsufficientCredits(0))
    assert "different URL" not in text
    assert "run out of credits" in text
    assert f"{settings.account_url.rstrip('/')}/pricing" in text
    # AI app directories reject upsell and billing prompts inside tool results.
    assert "top up" not in text and "upgrade" not in text and "/app/billing" not in text


@pytest.mark.anyio
async def test_a_keyless_call_also_explains_the_claude_app_route(
    monkeypatch: pytest.MonkeyPatch, charges: list[dict[str, Any]]
) -> None:
    """The Claude app has no `claude` command; its connector takes the sign-in address."""
    _install_key(monkeypatch, None)
    async with connected(token=None) as client:
        result = await client.call_tool("scrape", {"url": "https://example.com/p"})
    text = result.content[0].text  # type: ignore[union-attr]
    assert "Add custom connector" in text
    assert "https://engine.test/mcp-oauth" in text
    assert "create a free account" in text
