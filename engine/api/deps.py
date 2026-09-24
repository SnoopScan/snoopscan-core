"""API dependencies: auth, rate limiting, shared service instances."""

from __future__ import annotations

import time
from typing import Annotated

import structlog
from fastapi import Depends, Header, Request

from engine.api import onboarding, scopes
from engine.core.errors import ForbiddenScope, RateLimited, Unauthorized
from engine.core.fetch.base import Fetcher
from engine.core.fetch.tier0_http import HttpFetcher
from engine.core.fetch.tier1_impersonate import ImpersonateFetcher
from engine.core.models import Tier
from engine.core.politeness import RateLimiter
from engine.core.scrape_service import ScrapeService
from engine.storage import repositories as repo
from engine.storage.repositories import ApiKey

logger = structlog.get_logger(__name__)

_fetchers: dict[Tier, Fetcher] | None = None
_service: ScrapeService | None = None
_limiter = RateLimiter()


def _browser_fetcher() -> Fetcher | None:
    """Tier 2, if this deployment has it. None is a normal answer.

    Imported inside the function for two independent reasons, either of which
    alone would require it:

    1. The browser tier is proprietary and this module is public core. A
       module-scope import would break the open-core boundary and make the
       published package fail on import.
    2. Patchright is an optional extra. A self-hoster scraping ordinary pages
       should not be made to download ~400MB of Chromium to run the engine.

    Returning None rather than raising is the point — `ladder_from()` already
    falls back to the best available tier, so a deployment without a browser
    degrades to tiers 0/1 instead of failing. That path is exercised: the
    checkpoint measures 79% usable without a browser at all.
    """
    try:
        from engine.core.fetch.tier2_browser import BrowserFetcher
    except ImportError as exc:  # proprietary module or patchright absent
        logger.info("browser_tier_unavailable", reason=str(exc)[:120])
        return None
    return BrowserFetcher()


def get_fetchers() -> dict[Tier, Fetcher]:
    """The tier ladder. The escalation controller simply sees more rungs when
    a browser is available, and one fewer when it is not."""
    global _fetchers
    if _fetchers is None:
        _fetchers = {
            Tier.HTTP: HttpFetcher(),
            Tier.IMPERSONATE: ImpersonateFetcher(),
        }
        browser = _browser_fetcher()
        if browser is not None:
            _fetchers[Tier.BROWSER] = browser
            logger.info("browser_tier_enabled")
        for tier, fetcher in _stealth_fetchers().items():
            _fetchers[tier] = fetcher
            logger.info("stealth_tier_enabled", tier=str(tier))
    return _fetchers


def _stealth_fetchers() -> dict[Tier, Fetcher]:
    """Tiers 3, 3h and 4, each offered only if its module imports and its
    switch is on. Same deferred-import reasoning as the browser tier: these are
    proprietary, and Camoufox is a separate ~200MB download a self-hoster of
    the open core never needs."""
    from engine.settings import settings

    out: dict[Tier, Fetcher] = {}
    if settings.stealth_enabled:
        try:
            from engine.core.fetch.tier3_stealth import StealthFetcher

            out[Tier.STEALTH] = StealthFetcher()
        except ImportError as exc:
            logger.warning("stealth_tier_unavailable", reason=str(exc)[:120])
    if settings.stealth_hard_enabled:
        try:
            from engine.core.fetch.tier3h_camoufox import CamoufoxFetcher, MobileFetcher

            if CamoufoxFetcher.installed():
                out[Tier.STEALTH_HARD] = CamoufoxFetcher()
                out[Tier.MOBILE] = MobileFetcher()
            else:
                # WARNING, not info. Camoufox's browser build lives in the OS
                # cache directory (`~/Library/Caches/camoufox`), which cleaning
                # tools are entitled to empty — one did. Losing this drops the
                # only two rungs that pass DataDome, and the engine then
                # answered BLOCKED for every such domain for an hour because
                # the loss was one info line among thousands (7 Sep 2026).
                logger.warning(
                    "stealth_hard_tier_unavailable",
                    reason="camoufox browser build missing",
                    remedy="python -m camoufox fetch",
                    impact="DataDome domains will report BLOCKED",
                )
        except ImportError as exc:
            logger.warning(
                "stealth_hard_tier_unavailable",
                reason=str(exc)[:120],
                impact="DataDome domains will report BLOCKED",
            )
    return out


def get_service() -> ScrapeService:
    global _service
    if _service is None:
        _service = ScrapeService(get_fetchers())
    return _service


async def require_api_key(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> ApiKey:
    # Every refusal says how to get a key, so an agent can tell the person
    # rather than invent a placeholder. See engine/api/onboarding.py.
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized(onboarding.missing_key_message(), onboarding.detail())
    plaintext = authorization.split(" ", 1)[1].strip()
    if not plaintext:
        raise Unauthorized(onboarding.missing_key_message(), onboarding.detail())

    key = await repo.get_api_key(plaintext)
    if key is None:
        raise Unauthorized(onboarding.invalid_key_message(), onboarding.detail())
    # A key with no owner cannot be metered: `charge` has nothing to attribute
    # the spend to and it would fetch for free forever. Keys minted before
    # `owner_ref` was mandatory are refused here rather than quietly indulged.
    if key.owner_ref is None:
        logger.warning("ownerless_key_refused", key_id=key.id, label=key.label)
        raise Unauthorized("This key predates owner attribution; reissue it")

    allowed, remaining, reset = await _limiter.check(key.id, key.rate_limit_rpm)
    # Headers go on every response, including the 429.
    request.state.rate_limit = (key.rate_limit_rpm, remaining, reset)
    if not allowed:
        raise RateLimited(retry_after_s=max(1, reset - int(time.time())))

    # Scope, at the chokepoint. Every authenticated route depends on this
    # function, so a check here cannot be forgotten on the next endpoint the
    # way a per-route check can. An unmapped /v1 path denies: a new route is
    # unreachable until scopes.py names it, which is the deliberate act that
    # grants access.
    needed = scopes.required_for(request.url.path)
    if needed is not None and needed not in (key.scopes or []):
        logger.info(
            "scope_denied",
            key_id=key.id,
            path=request.url.path,
            needed=needed,
            held=list(key.scopes or []),
        )
        raise ForbiddenScope(needed)

    await repo.touch_api_key(key.id)
    return key


ApiKeyDep = Annotated[ApiKey, Depends(require_api_key)]


async def require_internal_token(
    x_internal_token: Annotated[str | None, Header()] = None,
) -> None:
    """The operator app's door. A constant-time compare against a shared secret;
    an empty secret means the /internal surface does not exist."""
    import hmac

    from engine.settings import get_settings

    expected = get_settings().internal_token
    if not expected or not x_internal_token or not hmac.compare_digest(x_internal_token, expected):
        raise Unauthorized("Missing or invalid internal token")


InternalDep = Annotated[None, Depends(require_internal_token)]
ServiceDep = Annotated[ScrapeService, Depends(get_service)]
