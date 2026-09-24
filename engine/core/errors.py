"""Error taxonomy from 01-api-surface.md.

TARGET_ERROR is deliberately distinct from BLOCKED: a genuine 404 must never
trigger tier escalation or proxy retirement.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    ENGINE_REFUSED = "ENGINE_REFUSED"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN_SCOPE = "FORBIDDEN_SCOPE"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    BLOCKED = "BLOCKED"
    FETCH_FAILED = "FETCH_FAILED"
    TARGET_ERROR = "TARGET_ERROR"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    ROBOTS_DENIED = "ROBOTS_DENIED"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    INTERNAL = "INTERNAL"
    INSUFFICIENT_CREDITS = "INSUFFICIENT_CREDITS"
    PROXY_UNAVAILABLE = "PROXY_UNAVAILABLE"
    SEARCH_UNAVAILABLE = "SEARCH_UNAVAILABLE"
    PLACES_UNAVAILABLE = "PLACES_UNAVAILABLE"
    SERP_UNAVAILABLE = "SERP_UNAVAILABLE"
    COMPANY_UNAVAILABLE = "COMPANY_UNAVAILABLE"
    PLATFORMS_UNAVAILABLE = "PLATFORMS_UNAVAILABLE"


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INSUFFICIENT_CREDITS: 402,
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.ENGINE_REFUSED: 503,
    ErrorCode.UNAUTHORIZED: 401,
    # The key is valid; it simply may not call this. 403, not 401 — retrying
    # with the same key will never work, and a 401 invites a caller to.
    ErrorCode.FORBIDDEN_SCOPE: 403,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.BLOCKED: 502,
    ErrorCode.FETCH_FAILED: 502,
    ErrorCode.TARGET_ERROR: 502,
    ErrorCode.EXTRACTION_FAILED: 500,
    ErrorCode.ROBOTS_DENIED: 403,
    ErrorCode.JOB_NOT_FOUND: 404,
    ErrorCode.INTERNAL: 500,
    ErrorCode.PROXY_UNAVAILABLE: 503,
    ErrorCode.SEARCH_UNAVAILABLE: 503,
    ErrorCode.PLACES_UNAVAILABLE: 503,
    ErrorCode.SERP_UNAVAILABLE: 503,
    ErrorCode.COMPANY_UNAVAILABLE: 503,
    ErrorCode.PLATFORMS_UNAVAILABLE: 503,
}


class EngineError(Exception):
    """Base error carrying an API error code and structured detail."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail: dict[str, Any] = detail or {}

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]

    def to_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": str(self.code), "message": self.message}
        if self.detail:
            body["detail"] = self.detail
        return body


class InvalidRequest(EngineError):
    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.INVALID_REQUEST, message, detail)


class Unauthorized(EngineError):
    def __init__(
        self, message: str = "Missing or invalid API key", detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(ErrorCode.UNAUTHORIZED, message, detail)


class ForbiddenScope(EngineError):
    """A valid key without the scope this route needs."""

    def __init__(self, scope: str) -> None:
        super().__init__(
            ErrorCode.FORBIDDEN_SCOPE,
            f"This API key does not carry the '{scope}' scope. "
            f"Add it to the key in your dashboard, or use a key that has it.",
        )


class RateLimited(EngineError):
    def __init__(self, retry_after_s: int) -> None:
        super().__init__(
            ErrorCode.RATE_LIMITED,
            "Rate limit exceeded",
            {"retry_after": retry_after_s},
        )
        self.retry_after_s = retry_after_s


class InsufficientCredits(EngineError):
    """The key's credit balance is exhausted. Nothing was fetched."""

    def __init__(self, remaining: int) -> None:
        super().__init__(
            ErrorCode.INSUFFICIENT_CREDITS,
            "Insufficient credits",
            {"credits_remaining": remaining},
        )


class ProxyUnavailable(EngineError):
    """An explicitly requested proxy could not be supplied. Nothing was fetched.

    This refusal exists because the alternative is worse. A caller who asks for
    `proxy: "residential"` is making a statement about which IP the target is
    allowed to see; falling back to a direct fetch answers a question they did
    not ask and exposes their own address to the target. Silently, and after
    the request has already left. So every path that cannot supply the
    requested proxy stops here instead.

    503, not 502: the target was never contacted, and the condition is
    ours and usually temporary.
    """

    def __init__(self, reason: str, proxy_type: str | None = None) -> None:
        detail: dict[str, Any] = {"reason": reason}
        if proxy_type:
            detail["proxy"] = proxy_type
        super().__init__(
            ErrorCode.PROXY_UNAVAILABLE,
            "No proxy available for this request; nothing was fetched",
            detail,
        )


class PlacesUnavailable(EngineError):
    """The Places source is not part of this deployment. Nothing was charged.

    503 for the same reason as search: nothing is broken. The open core does
    not ship the module — it sits behind the proxy layer and leadgen on the
    proprietary side — so a self-hoster reaching this endpoint has hit a
    boundary, not a bug.
    """

    def __init__(self, message: str = "Places is not available on this deployment") -> None:
        super().__init__(ErrorCode.PLACES_UNAVAILABLE, message, {"reason": "places_unavailable"})


class SerpUnavailable(EngineError):
    """Google results pages could not be served. Nothing was charged.

    503: either this deployment has no results provider configured, or the
    provider refused or failed this call. Neither is the caller's fault, and a
    retry later — or a configured provider — is the fix.
    """

    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.SERP_UNAVAILABLE, message, {"reason": "serp_unavailable"})


class CompanyUnavailable(EngineError):
    """Company enrichment is not part of this deployment. Nothing was charged.

    503, same as Places and for the same reason: the discovery and
    firmographics code sits behind leadgen on the proprietary side, so the open
    core reaching this endpoint has hit a boundary, not a bug.
    """

    def __init__(
        self, message: str = "Company enrichment is not available on this deployment"
    ) -> None:
        super().__init__(ErrorCode.COMPANY_UNAVAILABLE, message, {"reason": "company_unavailable"})


class PlatformsUnavailable(EngineError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.PLATFORMS_UNAVAILABLE,
            "Platform shortcuts are not available on this deployment",
            {"reason": "platforms_unavailable"},
        )


class SearchUnavailableError(EngineError):
    """No rung of the search ladder could answer. Nothing was charged.

    503 rather than 500: nothing is broken. Either every provider is refusing
    us at once, or the caller asked for something no configured provider can
    honour — a mobile SERP with no bought rung, say. Both are our configuration
    and both are usually temporary, and neither is an internal fault the caller
    should read as "try the same thing again".
    """

    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.SEARCH_UNAVAILABLE, message, {"reason": "search_unavailable"})


# Signals that describe OUR OWN refusal, not the target's. A verdict carrying
# one of these means the request never reached the site, or stopped for a rule
# of ours — so the answer must not begin "Target returned…".
#
# This is the second time the same mistake has been fixed here: THIN was split
# out of Blocked with the note "Saying BLOCKED here sent people chasing the
# wrong cause." It happened again with `circuit_open`, which cost a colleague
# an afternoon chasing IMDb for our own breaker, so it is a SET now rather
# than another special case.
ENGINE_SIDE_SIGNALS: frozenset[str] = frozenset(
    {
        "circuit_open",  # our breaker is open for this domain
        "url_backoff_open",  # ...for this one url; the rest of the site is fine
        "deadline_exceeded",  # our time budget, not their latency
        "no_tier_available",  # the rung it needed is disabled or missing here
        "tier_ceiling_below_floor",  # the CALLER's maxTier forbade the only rung
        "not_attempted",  # we never sent a request at all
        "politeness_timeout",  # our pacing gate, waiting on ourselves
    }
)

_ENGINE_SIDE_MESSAGE: dict[str, str] = {
    "circuit_open": (
        "SnoopScan is not currently attempting this domain: too many recent "
        "failures tripped our circuit breaker, and it stays open for "
        "{minutes} minutes. The site was not contacted for this request. "
        "Retry after the window, or pass maxAge to read a cached copy."
    ),
    "url_backoff_open": (
        "SnoopScan is not currently attempting this URL: it has failed "
        "repeatedly, so it is backed off for {minutes} minutes. The rest of "
        "the site is unaffected and can still be scraped. The page was not "
        "contacted for this request."
    ),
    "deadline_exceeded": (
        "SnoopScan ran out of its own time budget before the page finished. "
        "The site did not refuse us. Raise `timeout`, or ask for fewer formats."
    ),
    "tier_ceiling_below_floor": (
        "This page needs a more expensive fetch tier than `maxTier` allows, so "
        "SnoopScan did not attempt it and nothing was charged. The site did not "
        "refuse us. Raise `maxTier`, or leave it unset to let the engine choose."
    ),
    "no_tier_available": (
        "SnoopScan has no fetch tier available that can serve this request. "
        "This is a configuration problem on our side, not a refusal by the site."
    ),
    "not_attempted": ("SnoopScan did not attempt this request. The site was not contacted."),
    "politeness_timeout": (
        "SnoopScan waited for its own per-host pacing slot and gave up. The "
        "site did not refuse us — slow down, or raise the plan's concurrency."
    ),
}


class EngineRefused(EngineError):
    """We stopped, and the target had nothing to do with it.

    Separate from Blocked so the message can never read as the site's doing,
    and separate from a 5xx crash so a caller can tell "try later" from
    "something broke".
    """

    def __init__(
        self,
        signal: str,
        tiers_attempted: list[str] | None = None,
        **hints: Any,
    ) -> None:
        template = _ENGINE_SIDE_MESSAGE.get(
            signal, "SnoopScan stopped before reaching the site ({signal})."
        )
        detail: dict[str, Any] = {"final_signal": signal, "engine_side": True}
        if tiers_attempted:
            detail["tiers_attempted"] = tiers_attempted
        detail.update(hints)
        super().__init__(
            ErrorCode.ENGINE_REFUSED,
            template.format(signal=signal, **hints),
            detail,
        )


class Blocked(EngineError):
    def __init__(
        self,
        message: str,
        tiers_attempted: list[str] | None = None,
        final_signal: str | None = None,
    ) -> None:
        detail: dict[str, Any] = {}
        if tiers_attempted:
            detail["tiers_attempted"] = tiers_attempted
        if final_signal:
            detail["final_signal"] = final_signal
        super().__init__(ErrorCode.BLOCKED, message, detail)


class FetchFailed(EngineError):
    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.FETCH_FAILED, message, detail)


class TargetError(EngineError):
    def __init__(self, status_code: int | None, message: str | None = None) -> None:
        super().__init__(
            ErrorCode.TARGET_ERROR,
            message or f"Target returned status {status_code}",
            {"status_code": status_code},
        )


class ExtractionFailed(EngineError):
    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.EXTRACTION_FAILED, message, detail)


class RobotsDenied(EngineError):
    def __init__(self, url: str) -> None:
        super().__init__(
            ErrorCode.ROBOTS_DENIED,
            "robots.txt disallows fetching this URL",
            {"url": url},
        )


class JobNotFound(EngineError):
    def __init__(self, job_id: str) -> None:
        super().__init__(ErrorCode.JOB_NOT_FOUND, "Unknown job id", {"job_id": job_id})


class Timeout(EngineError):
    def __init__(self, message: str = "Fetch exceeded the allotted timeout") -> None:
        super().__init__(ErrorCode.TIMEOUT, message)
