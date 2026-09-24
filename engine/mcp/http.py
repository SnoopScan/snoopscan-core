"""The hosted MCP endpoint: streamable HTTP with a bearer API key.

08-mcp-server.md section 7. The same `api_keys` table as REST, the same
rate limit, the same metering: a customer's agent spends the customer's
credits exactly as their code would. Guardrails are per MCP session, keyed
by the `Mcp-Session-Id` the transport assigns, so one runaway agent cannot
spend another's budget or its own twice over.

The tools themselves live in `engine.mcp.server` and know nothing about
transport; they ask this module who is calling.
"""

from __future__ import annotations

import contextvars
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from engine.api import deps, onboarding
from engine.core.errors import EngineError
from engine.mcp import guardrails as guard
from engine.settings import settings
from engine.storage.repositories import ApiKey

logger = structlog.get_logger(__name__)

_key_var: contextvars.ContextVar[ApiKey | None] = contextvars.ContextVar("mcp_key", default=None)
_budget_var: contextvars.ContextVar[guard.SessionBudget | None] = contextvars.ContextVar(
    "mcp_budget", default=None
)


def current_key() -> ApiKey | None:
    """The API key behind this request, or None on stdio (trusted by process boundary)."""
    return _key_var.get()


def current_budget() -> guard.SessionBudget | None:
    """This MCP session's guardrail budget, or None on stdio."""
    return _budget_var.get()


class SessionBudgets:
    """One `SessionBudget` per MCP session, forgotten after an idle hour.

    A session is the transport's `Mcp-Session-Id`; before the client has one
    (the initialize request) the key itself stands in.
    """

    def __init__(self, idle_seconds: int = 3600, max_sessions: int = 5000) -> None:
        self._budgets: dict[str, tuple[guard.SessionBudget, float]] = {}
        self.idle_seconds = idle_seconds
        self.max_sessions = max_sessions

    def get(self, session: str) -> guard.SessionBudget:
        now = time.monotonic()
        if len(self._budgets) >= self.max_sessions:
            self._prune(now)
        budget, _ = self._budgets.get(session, (None, 0.0))
        if budget is None:
            budget = guard.SessionBudget()
        self._budgets[session] = (budget, now)
        return budget

    def _prune(self, now: float) -> None:
        stale = [s for s, (_, seen) in self._budgets.items() if now - seen > self.idle_seconds]
        for s in stale:
            del self._budgets[s]

    def __len__(self) -> int:
        return len(self._budgets)


class BearerKeyMiddleware:
    """Require `Authorization: Bearer <api key>` and set who is calling for the tools.

    Refusals use the REST envelope so a developer reading the response sees the
    same shape they know from `/v1`.
    """

    def __init__(self, app: ASGIApp, budgets: SessionBudgets, *, sign_in: bool = False) -> None:
        self.app = app
        self.budgets = budgets
        # The /mcp-oauth door: no key means "sign in", said the OAuth way, so
        # an app with no box for a key (the Claude app's connectors) opens the
        # browser sign-in instead of connecting keyless.
        self.sign_in = sign_in

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        try:
            key = await deps.require_api_key(request, request.headers.get("authorization"))
        except EngineError as exc:
            if exc.http_status == 401 and not self.sign_in:
                await _keyless(self.app, scope, receive, send, request)
                return
            headers = None
            if exc.http_status == 401:
                metadata = f"{_origin(request)}{PROTECTED_RESOURCE_PATH}{SIGN_IN_PATH}"
                headers = {"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'}
            response = JSONResponse(
                {"success": False, "error": {"code": str(exc.code), "message": exc.message}},
                status_code=exc.http_status,
                headers=headers,
            )
            await response(scope, receive, send)
            return
        session = request.headers.get("mcp-session-id") or f"key:{key.id}"
        budget = self.budgets.get(session)
        key_token = _key_var.set(key)
        budget_token = _budget_var.set(budget)
        try:
            await self.app(scope, receive, send)
        finally:
            _key_var.reset(key_token)
            _budget_var.reset(budget_token)


# What a caller with no working key may do: connect, and see what the server
# offers. Anything that would run a tool, read data or render a prompt is
# answered at the door with how to get a key — never passed to the server,
# whose tools read "no key" as the trusted local (stdio) transport and would
# otherwise run unmetered.
_KEYLESS_METHODS = frozenset(
    {"initialize", "ping", "tools/list", "prompts/list", "resources/list",
     "resources/templates/list"}
)  # fmt: skip


SIGN_IN_PATH = "/mcp-oauth"
PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"


def _origin(request: Request) -> str:
    host = request.headers.get("host") or "localhost"
    local = host.startswith(("localhost", "127.", "[::1]"))
    return f"{'http' if local else 'https'}://{host}"


def _public_endpoint(request: Request) -> str:
    return f"{_origin(request)}/mcp"


def protected_resource(request: Request) -> JSONResponse:
    """RFC 9728: where an app signs in to use /mcp-oauth — the account site."""
    return JSONResponse(
        {
            "resource": f"{_origin(request)}{SIGN_IN_PATH}",
            "authorization_servers": [settings.account_url.rstrip("/")],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["snoopscan"],
            "resource_documentation": f"{settings.account_url.rstrip('/')}/integrations",
        },
        headers={"Access-Control-Allow-Origin": "*"},
    )


def _allowed(message: Any) -> bool:
    method = message.get("method", "") if isinstance(message, dict) else ""
    return method in _KEYLESS_METHODS or method.startswith("notifications/")


def _refusal(message: Any, text: str) -> dict[str, Any] | None:
    """The JSON-RPC answer to one message a keyless caller may not make."""
    if not isinstance(message, dict) or "id" not in message:
        return None  # a notification: nothing to answer
    if message.get("method") == "tools/call":
        # A tool RESULT, not a protocol error: agents show a result's text to the
        # model, which is who has to read the instructions and pass them on.
        result = {"content": [{"type": "text", "text": text}], "isError": True}
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}
    return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32001, "message": text}}


async def _keyless(
    app: ASGIApp, scope: Scope, receive: Receive, send: Send, request: Request
) -> None:
    """Let a keyless client connect and list tools; answer every call with setup steps.

    A bare 401 here left Claude Code with "failed to connect" and the agent
    with nothing to tell the person. Now the server connects, its tools are
    listed, and the first call returns the words to say and the command that
    reconnects it with a real key.
    """
    body = await request.body()
    if request.method == "POST":
        try:
            parsed = json.loads(body or b"null")
        except ValueError:
            parsed = None
        messages = parsed if isinstance(parsed, list) else [parsed]
        if not all(_allowed(m) for m in messages):
            text = onboarding.mcp_setup_text(
                _public_endpoint(request), had_key=bool(request.headers.get("authorization"))
            )
            answers = [a for a in (_refusal(m, text) for m in messages) if a is not None]
            logger.info("mcp_keyless_call_refused", methods=[
                m.get("method") for m in messages if isinstance(m, dict)])  # fmt: skip
            payload: Any = answers if isinstance(parsed, list) else (answers[0] if answers else {})
            await JSONResponse(payload, status_code=200 if answers else 202)(scope, receive, send)
            return

    delivered = False

    async def replay() -> Any:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    await app(scope, replay, send)


budgets = SessionBudgets()
_server: Any = None
_transport: StreamableHTTPASGIApp | None = None


class _Transport:
    """The `/mcp` handler. The session manager behind it lives for one run of the
    process lifespan; a fresh one is made each time the lifespan starts, since a
    manager can only be run once."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if _transport is None:
            response = JSONResponse(
                {
                    "success": False,
                    "error": {"code": "INTERNAL", "message": "The MCP endpoint is starting."},
                },
                status_code=503,
            )
            await response(scope, receive, send)
            return
        await _transport(scope, receive, send)


def build_asgi(server: Any, *, sign_in: bool = False) -> ASGIApp:
    """The ASGI app for `/mcp` (or, with sign_in, `/mcp-oauth`): auth, then the transport."""
    global _server
    _server = server
    return BearerKeyMiddleware(_Transport(), budgets, sign_in=sign_in)


@asynccontextmanager
async def lifespan() -> AsyncIterator[None]:
    """Run a session manager for the life of the API process."""
    global _transport
    if _server is None:
        yield
        return
    # streamable_http_app() is what creates a session manager; the Starlette app
    # it returns is not used — the route is registered on the API itself.
    _server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        host="0.0.0.0",  # noqa: S104 — not a bind; it only keeps localhost DNS-rebinding rules off a public endpoint
    )
    manager: StreamableHTTPSessionManager = _server.session_manager
    _transport = StreamableHTTPASGIApp(manager)
    try:
        async with manager.run():
            yield
    finally:
        _transport = None
