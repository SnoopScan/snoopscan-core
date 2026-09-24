"""Boilerplate sweep (04-extraction.md section 6).

Post-extraction pass removing what survived. Applied conservatively:
removing genuine content is worse than leaving a stray cookie line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

_PATH = Path(__file__).resolve().parent / "boilerplate.yaml"

# Three or more consecutive short link-only lines is a navigation run.
_LINK_ONLY = re.compile(r"^\s*[-*]?\s*\[[^\]]{1,60}\]\([^)]+\)\s*$")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")


@dataclass(frozen=True)
class BoilerplateRules:
    phrases: tuple[str, ...]
    trailing_headings: tuple[str, ...]
    max_line_chars: int


@lru_cache(maxsize=1)
def _rules() -> BoilerplateRules:
    raw = yaml.safe_load(_PATH.read_text()) or {}
    return BoilerplateRules(
        phrases=tuple(str(p).lower() for p in raw.get("phrases", [])),
        trailing_headings=tuple(str(h).lower() for h in raw.get("trailing_section_headings", [])),
        max_line_chars=int(raw.get("max_line_chars", 200)),
    )


def reload_rules() -> None:
    _rules.cache_clear()


def _is_boilerplate_line(line: str, rules: BoilerplateRules) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > rules.max_line_chars:
        return False
    lowered = stripped.lower().lstrip("#*-_> ").strip()
    return any(lowered.startswith(p) or lowered == p for p in rules.phrases)


def _trim_trailing_sections(lines: list[str], rules: BoilerplateRules) -> list[str]:
    """Drop a recirculation section, but only when it sits in the tail.

    A "Related articles" heading a third of the way down is part of the page's
    real structure; the same heading at 85% is recirculation.
    """
    if len(lines) < 12:
        return lines
    cutoff_start = int(len(lines) * 0.6)
    for index in range(cutoff_start, len(lines)):
        match = _HEADING.match(lines[index].strip())
        if not match:
            continue
        heading = match.group(1).strip().lower().rstrip(":")
        if any(heading == h or heading.startswith(h) for h in rules.trailing_headings):
            return lines[:index]
    return lines


def _drop_nav_runs(lines: list[str]) -> list[str]:
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        # Three or more consecutive link-only lines is a nav block, not content.
        if len(run) < 3:
            out.extend(run)
        run.clear()

    for line in lines:
        if _LINK_ONLY.match(line):
            run.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return out


# A value this short, repeated under a heading of its own, is a field of an
# entry — every company on a register is "Active" — not an extraction artefact.
ENTRY_FIELD_MAX = 120


def is_heading(text: str) -> bool:
    return text.lstrip().startswith("#")


def entry_field(text: str, heading: str | None, headings_seen: set[str]) -> bool:
    """A short line under a heading that has not been seen before.

    A page rendered twice repeats its HEADINGS too, so a repeat under a
    repeated (or absent) heading is still dropped; a repeat under a new
    heading is the same field of the next entry.
    """
    return (
        heading is not None
        and heading not in headings_seen
        and not is_heading(text)
        and len(text) <= ENTRY_FIELD_MAX
    )


def _drop_repeated_lines(lines: list[str]) -> list[str]:
    """Remove lines repeated verbatim many times — an artifact of failed
    extraction. Short structural lines are exempt, and so is a short field
    repeated once per entry of a listing (see entry_field)."""
    counts: dict[str, int] = {}
    for line in lines:
        key = line.strip()
        if len(key) > 15:
            counts[key] = counts.get(key, 0) + 1
    repeated = {k for k, v in counts.items() if v >= 4}
    if not repeated:
        return lines
    seen: set[str] = set()
    in_section: set[str] = set()
    heading: str | None = None
    fresh = False
    headings_seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.strip()
        if is_heading(key):
            fresh = key not in headings_seen
            headings_seen.add(key)
            heading, in_section = key, set()
        if key in repeated:
            field = fresh and entry_field(key, heading, set()) and key not in in_section
            if key in seen and not field:
                continue
            seen.add(key)
            in_section.add(key)
        out.append(line)
    return out


def sweep(markdown: str) -> str:
    """Run the full conservative sweep over extracted markdown."""
    if not markdown:
        return markdown
    rules = _rules()
    lines = markdown.split("\n")

    lines = _trim_trailing_sections(lines, rules)
    lines = [line for line in lines if not _is_boilerplate_line(line, rules)]
    lines = _drop_nav_runs(lines)
    lines = _drop_repeated_lines(lines)

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def boilerplate_hits(markdown: str) -> int:
    """How many boilerplate phrases survive — an input to confidence scoring."""
    rules = _rules()
    lowered = markdown.lower()
    return sum(1 for phrase in rules.phrases if phrase in lowered)
