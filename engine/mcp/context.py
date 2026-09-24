"""Context-window discipline (08-mcp-server.md section 1).

The single most important design constraint of the MCP surface, and where most
MCP wrappers fail: a 50,000-word page returned in full fills the agent's
context and destroys the session.

Truncation is always VISIBLE. Silent truncation makes an agent confidently
wrong about content it never saw, which is worse than returning nothing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_CHARS = 20_000
DEFAULT_MAX_CHARS_PER_RESULT = 5_000
DEFAULT_MAX_CHARS_PER_PAGE = 5_000

# Continuation tokens live in the process for the session's lifetime. They are
# a convenience for reading the rest of one page, not durable state — the
# content itself is already in Postgres.
_CONTINUATIONS: dict[str, str] = {}
_MAX_CONTINUATIONS = 200


@dataclass
class Truncation:
    text: str
    truncated: bool
    token: str | None = None
    total_chars: int = 0
    returned_chars: int = 0


def _token_for(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def truncate(content: str | None, max_chars: int = DEFAULT_MAX_CHARS) -> Truncation:
    """Cut content to a budget, always saying so.

    The marker names both halves of the number so the agent can decide whether
    the remainder is worth fetching, and carries the token that fetches it.
    """
    if content is None:
        return Truncation(text="", truncated=False)

    total = len(content)
    if total <= max_chars:
        return Truncation(text=content, truncated=False, total_chars=total, returned_chars=total)

    head = content[:max_chars]
    remainder = content[max_chars:]
    token = _token_for(content)

    if len(_CONTINUATIONS) >= _MAX_CONTINUATIONS:
        _CONTINUATIONS.pop(next(iter(_CONTINUATIONS)))
    _CONTINUATIONS[token] = remainder

    marker = (
        f"\n\n...[truncated: {max_chars:,} of {total:,} chars returned. "
        f"Use fetchMore with token '{token}' for the remainder]"
    )
    return Truncation(
        text=head + marker,
        truncated=True,
        token=token,
        total_chars=total,
        returned_chars=max_chars,
    )


def continuation(token: str, max_chars: int = DEFAULT_MAX_CHARS) -> Truncation:
    """Read the next slice of a previously truncated body."""
    remainder = _CONTINUATIONS.get(token)
    if remainder is None:
        return Truncation(text="", truncated=False)
    result = truncate(remainder, max_chars)
    if not result.truncated:
        _CONTINUATIONS.pop(token, None)
    return result


def clear_continuations() -> None:
    _CONTINUATIONS.clear()


def compact_cost(cost: Any) -> str:
    """One line an agent can read, rather than the full cost object.

    Keeps the honest-cost guarantee visible without spending context on fields
    a model has no use for.
    """
    if cost is None:
        return ""
    if getattr(cost, "cached", False):
        return "cost: served from cache (no fetch)"

    parts = [f"tier {getattr(cost, 'tier', 'unknown')}"]
    attempted = getattr(cost, "tiers_attempted", []) or []
    if len(attempted) > 1:
        parts.append(f"escalated through {' -> '.join(attempted)}")
    proxy_bytes = getattr(cost, "proxy_bytes", 0) or 0
    if proxy_bytes:
        parts.append(f"{proxy_bytes / 1024:.0f}KB proxy")
    browser_ms = getattr(cost, "browser_ms", 0) or 0
    if browser_ms:
        parts.append(f"{browser_ms}ms browser")
    return "cost: " + ", ".join(parts)
