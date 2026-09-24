"""Model-facing error text (08-mcp-server.md section 6).

Errors here are written for a model to act on, not for a developer to grep.
An agent given a bare error code retries the identical request; one told what
failed, why, and what to try instead adapts.

Bad:  "Error: BLOCKED"
Good: what happened, why, and two concrete alternatives.
"""

from __future__ import annotations

from engine.core.errors import EngineError, ErrorCode
from engine.settings import settings

_GUIDANCE: dict[ErrorCode, str] = {
    ErrorCode.BLOCKED: (
        "Could not fetch this page — the site blocked the request at every method "
        "tried. This site has strong bot protection. Options: try a different source "
        "for this information, or check whether the content is available through the "
        "site's own API or RSS feed."
    ),
    ErrorCode.TARGET_ERROR: (
        "The site responded, but not with a page. This is the site's own error, not a "
        "block — the URL is probably wrong or the page has been removed. Check the URL, "
        "or use map to see what pages actually exist on this site."
    ),
    ErrorCode.FETCH_FAILED: (
        "Could not reach the site at all — the connection failed at the network level. "
        "The domain may be misspelled, or the site may be down. Verify the URL, and if "
        "it is right, try again shortly."
    ),
    ErrorCode.TIMEOUT: (
        "The page took too long to respond and the request was abandoned. Slow sites "
        "sometimes succeed on a second attempt; if it fails again, the page is probably "
        "too heavy to fetch and you should look for a lighter alternative."
    ),
    ErrorCode.ROBOTS_DENIED: (
        "This site's robots.txt asks crawlers not to fetch this path, so the request "
        "was not made. This is a policy decision, not a technical failure. Find the "
        "information from another source."
    ),
    ErrorCode.EXTRACTION_FAILED: (
        "The page was fetched but no readable content could be extracted from it. It is "
        "probably an app shell, a media page, or a document format rather than an "
        "article. Try a different URL on the same site."
    ),
    ErrorCode.INVALID_REQUEST: (
        "The request was rejected before anything was fetched. Check the URL is "
        "absolute and starts with http:// or https://, and that it points at a public "
        "site rather than an internal address."
    ),
    ErrorCode.RATE_LIMITED: (
        "Requests are being made faster than the rate limit allows. Wait a moment "
        "before the next call, and batch related work rather than fetching one page "
        "at a time."
    ),
    ErrorCode.JOB_NOT_FOUND: (
        "No job exists with that id. Check the id returned when the crawl was started; "
        "jobs are also purged after their retention period."
    ),
    ErrorCode.INTERNAL: (
        "Something failed inside the engine. This is our problem, not a problem with "
        "the URL. Retrying once is reasonable; if it fails again, report it and move on."
    ),
}


def _account_guidance(code: ErrorCode) -> str | None:
    """Errors only the PERSON can fix. "Try a different URL" sent an agent
    round every URL it could think of while the account sat at zero."""
    base = settings.account_url.rstrip("/")
    if code == ErrorCode.INSUFFICIENT_CREDITS:
        return (
            "This SnoopScan account has run out of credits, so nothing was fetched and "
            "nothing was charged. Retrying, or trying other URLs, will fail the same way. "
            f'Tell the user: "Your SnoopScan account has no credits left. Plans and '
            f'credits are explained at {base}/pricing."'
        )
    if code == ErrorCode.FORBIDDEN_SCOPE:
        return (
            "The API key this server uses is not allowed to call this tool. Retrying will "
            "not help. Tell the user the key needs this tool switched on: they can create "
            f"a key with every tool at {base}/app/keys, or reconnect SnoopScan."
        )
    return None


def explain(exc: EngineError) -> str:
    """Turn an engine error into something an agent can act on."""
    account = _account_guidance(exc.code)
    if account is not None:
        return account
    guidance = _GUIDANCE.get(exc.code)
    if guidance is None:
        return f"{exc.message}. Try a different URL or source."

    # Add the specific signal where it changes what the agent should do.
    if exc.code == ErrorCode.TARGET_ERROR:
        status = exc.detail.get("status_code")
        if status:
            return f"{guidance}\n\n(The site returned HTTP {status}.)"
    if exc.code == ErrorCode.BLOCKED:
        tiers = exc.detail.get("tiers_attempted") or []
        if tiers:
            return f"{guidance}\n\n(Methods tried: {', '.join(tiers)}.)"
    return guidance
