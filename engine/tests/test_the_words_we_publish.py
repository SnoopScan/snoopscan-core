"""The OpenAPI descriptions ARE the published API reference.

Every `description=` on a field and every model docstring is rendered into the
reference a customer reads. They are copy, not comments, and they were being
written like comments — `SearchRequest` told the world that a source which
cannot honour a filter "is dropped from the ladder", which is our word for the
fetch escalation order.

The app has `PlainWordsTest` guarding its own surfaces. This is the same guard
for the half of the reference that is generated from here, because copy that
lives in a .py file is the easiest kind to forget is copy.

Comments and docstrings that are NOT published stay as they are: internal
prose explaining why a thing works is the point of this codebase.
"""

from __future__ import annotations

import re

# Our words. None belong in a reference a customer reads.
HOUSE_JARGON = re.compile(
    r"\brungs?\b|\bladders?\b|\bescalat\w*|\bimpersonat\w*|\bcamoufox\b|\bpatchright\b"
    r"|under the hood|behind the scenes|\bour engine\b|\bthe fetcher\b",
    re.I,
)

# A caller CAN set tier/proxy values, so the names themselves have to be
# printable where they are the literal value of a field. What must not appear
# is our narration about them.
ALLOWED = re.compile(r"`(impersonate|stealth|stealth_hard|mobile|browser|http)`")


def _descriptions() -> list[tuple[str, str]]:
    from engine.api.app import app

    schema = app.openapi()
    found: list[tuple[str, str]] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "description" and isinstance(value, str):
                    found.append((path, value))
                else:
                    walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for item in node:
                walk(item, path)

    walk(schema, "")
    return found


def test_the_published_reference_carries_none_of_our_words() -> None:
    offenders = []
    for path, text in _descriptions():
        cleaned = ALLOWED.sub(" ", text)
        match = HOUSE_JARGON.search(cleaned)
        if match:
            offenders.append(f"{path}: {match.group(0)!r} in {text[:110]!r}")

    assert offenders == [], "\n".join(offenders)


def test_the_guard_can_actually_see_a_description() -> None:
    """A scan that walks the wrong shape passes on everything.

    The negative control belongs here rather than in a comment: this walker
    reads a nested OpenAPI document, and if the traversal ever stops matching
    it, the test above turns green for the wrong reason and stays green.
    """
    descriptions = _descriptions()

    assert len(descriptions) > 40, f"only found {len(descriptions)} descriptions"
    assert any("formats" in path for path, _ in descriptions)
