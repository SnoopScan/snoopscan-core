"""Change tracking (04-extraction.md section 8).

Answers "has this page changed since we last looked?" without storing a copy
of every version: `page_versions` is append-only and holds a hash per observed
change, and the diff is computed on demand from the two `pages` rows.

Normalisation before hashing is the part that matters. Without it a timestamp,
a view counter or a rotating advert makes every fetch look changed, and a
change feed that cries wolf is worse than none — people stop reading it.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

# Content that changes on every request without the page having changed.
_VOLATILE = (
    # Timestamps and relative times.
    re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b"),
    re.compile(r"\b\d+\s+(?:second|minute|hour|day)s?\s+ago\b", re.IGNORECASE),
    # View, comment and share counters.
    re.compile(r"\b[\d,]+\s+(?:views?|comments?|shares?|likes?|reads?)\b", re.IGNORECASE),
    # Cache-busting identifiers that leak into text.
    re.compile(r"\b[0-9a-f]{16,}\b", re.IGNORECASE),
)

_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION_RUN = re.compile(r"[^\w\s]{2,}")


class ChangeStatus(StrEnum):
    NEW = "new"
    CHANGED = "changed"
    SAME = "same"


@dataclass
class ChangeResult:
    status: ChangeStatus
    previous_scrape_at: datetime | None = None
    diff: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "changeStatus": str(self.status),
            "previousScrapeAt": (
                self.previous_scrape_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                if self.previous_scrape_at
                else None
            ),
        }
        if self.diff is not None:
            payload["diff"] = self.diff
        return payload


def normalise(text: str) -> str:
    """Strip what changes without the content changing.

    Applied before hashing. The spec is explicit that skipping this makes
    every fetch look changed.
    """
    cleaned = text.lower()
    for pattern in _VOLATILE:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = _PUNCTUATION_RUN.sub(" ", cleaned)
    return _WHITESPACE.sub(" ", cleaned).strip()


def git_diff(previous: str, current: str, *, context: int = 3) -> str:
    """A unified diff of the two markdown bodies. Cheap, no model call."""
    return "\n".join(
        difflib.unified_diff(
            previous.splitlines(),
            current.splitlines(),
            fromfile="previous",
            tofile="current",
            lineterm="",
            n=context,
        )
    )


def summarise(previous: str, current: str) -> dict[str, int]:
    """How much moved, without a model call.

    Enough for a caller to decide whether the diff is worth reading, which is
    what most consumers of a change feed actually want.
    """
    diff = list(difflib.unified_diff(previous.splitlines(), current.splitlines(), lineterm="", n=0))
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    return {"linesAdded": added, "linesRemoved": removed}
