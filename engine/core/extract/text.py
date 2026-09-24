"""One way to turn a DOM node into text, so word boundaries survive.

selectolax's ``node.text(deep=True, strip=True)`` concatenates descendant text
nodes with **no separator at all**, so neighbouring words weld into one token:

    <a>Baby name</a><a>Meaning of names</a>  ->  "Baby nameMeaning of names"

Measured on thebump.com/b/kassandra-baby-name (7 Sep 2026): nine welded tokens
in one page, including ``nameMeaning`` and ``historyBaby`` on a content
boundary. Anything matching word by word downstream — stated-meaning
extraction, ``names:check-facts`` — matches nothing against a welded token.

**The boundary is decided by position, not by tag name.** The obvious rule,
"separate block tags and not inline ones", fails on real pages: the welding
markup on thebump is a stack of ``<a>`` and ``<span>`` elements — inline by the
HTML spec, block-level flex items by their CSS, which we cannot see. What holds
without knowing the CSS is simpler:

* between two adjacent **element** siblings, insert a separator — whatever
  their tags, they are two things, and a browser gives them their own boxes
  far more often than not;
* between an element and adjacent **raw text**, insert nothing — that is a run
  of prose with markup inside it, so ``<span>Name</span>berry`` stays
  "Nameberry" and ``The name <b>Aspen</b> means`` keeps its single spaces.

Whitespace already in the source is preserved and collapsed at the end, rather
than stripped per-node — stripping first is what destroyed the real separators.

This is the only place that decision is made. Callers that want text from a
node call :func:`node_text`; nobody calls ``.text(deep=True)`` directly.
"""

from __future__ import annotations

import re

from selectolax.parser import Node

_TEXT_NODE = "-text"
_WHITESPACE = re.compile(r"\s+")
# Their text is markup, not content, and a browser renders none of it.
_NON_CONTENT = frozenset({"script", "style", "template", "noscript", "svg"})


def _collect(node: Node, out: list[str]) -> None:
    previous_was_element = False
    for child in node.iter(include_text=True):
        tag = child.tag
        if tag == _TEXT_NODE:
            out.append(child.text(deep=False) or "")
            previous_was_element = False
            continue
        if tag in _NON_CONTENT:
            # Skipped entirely, but it still separates what sits either side.
            out.append(" ")
            previous_was_element = True
            continue
        if previous_was_element:
            out.append(" ")
        _collect(child, out)
        previous_was_element = True


def node_text(node: Node | None, *, strip: bool = True) -> str:
    """Text of ``node`` and its descendants, with word boundaries preserved.

    Drop-in for ``node.text(deep=True, strip=True)``. With ``strip`` (the
    default) runs of whitespace collapse to a single space and the result is
    trimmed, which is what every current caller wants.
    """
    if node is None:
        return ""
    parts: list[str] = []
    _collect(node, parts)
    text = "".join(parts)
    if not strip:
        return text
    return _WHITESPACE.sub(" ", text).strip()
