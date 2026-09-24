"""The `summary` format: the gist of a page, without a model call.

`summary` was an accepted format, a documented one, a checkbox in our own
playground and a field on the response — and NOTHING ever set it. Every
request that asked for it was billed the normal fetch price and got `null`.
That is the same fault as `screenshot`, `quality`, `parsers` and scopes: a
declared capability with nothing behind it.

Deterministic and extractive on purpose. A model-written summary would be
better prose, but it would make the SAME request cost different amounts and
take different times on different deployments — and a self-hoster with no
provider key would be back to `null`. The docs promise "the gist, not the
page"; the lead of a well-written page is exactly that, and taking it costs
nothing, adds no latency and cannot invent a fact the page does not contain.

If a model summary is ever offered it should be a DIFFERENT, declared thing
(`{"type": "summary", "model": true}`) with its own price, not a silent
upgrade that changes the bill.
"""

from __future__ import annotations

import re

# Roughly two or three sentences. Long enough to say what the page is, short
# enough that a caller who wanted the page would have asked for the page.
DEFAULT_MAX_CHARS = 500

# A paragraph shorter than this is a caption, a byline or a cookie notice.
MIN_PARAGRAPH_WORDS = 8

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])[\s ]+")
_TERMINATED = re.compile(r"[.!?][\"')\]]?$")
# Markdown block markers: a line starting with one of these is not prose.
_NOT_PROSE = re.compile(r"^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s|\||```|~~~|!\[)")
_INLINE_IMAGE = re.compile(r"!\[(?:[^\[\]\\]|\\.)*\]\([^)]*\)")
# Link text may itself contain ESCAPED brackets — Wikipedia's citations arrive
# as `[\[1\]](url)` — so the inner group has to admit `\[` and `\]`. A plain
# `[^\]]*` stops at the first `]` and leaves the whole thing in the summary,
# which is how the first version of this shipped `<sup>[\[1\]](https://...)`
# as the opening sentence of a page.
_LINK_TEXT = re.compile(r"\[((?:[^\[\]\\]|\\.)*)\]\([^)]*\)")
# Raw HTML survives markdown conversion: <sup>, <sub>, <span>, <br>.
_HTML_TAG = re.compile(r"<[^>]{1,200}>")
# What is left of a citation once its link is gone: [1], [12], [note 3], [a].
_CITATION = re.compile(r"\s*\[\s*(?:\d{1,3}|[a-z]|note \d{1,3})\s*\]")
_ESCAPED = re.compile(r"\\([\[\]()*_`#~])")
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|`)")


def _blocks(markdown: str) -> list[str]:
    """Markdown paragraphs, in order, with fenced code removed."""
    out: list[str] = []
    fenced = False
    current: list[str] = []

    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            if current:
                out.append(" ".join(current))
                current = []
            continue
        if fenced:
            continue
        if not stripped:
            if current:
                out.append(" ".join(current))
                current = []
            continue
        if _NOT_PROSE.match(line):
            # A heading or list item ENDS the paragraph before it; it does not
            # join on, which would weld a heading to the sentence beneath it.
            if current:
                out.append(" ".join(current))
                current = []
            continue
        current.append(stripped)

    if current:
        out.append(" ".join(current))
    return out


def _plain(text: str) -> str:
    """Markdown down to the words a person would read aloud.

    Order matters: links go before citations, because a citation marker is
    usually the TEXT of a link and only becomes a bare `[1]` once the URL has
    been removed.
    """
    text = _INLINE_IMAGE.sub("", text)
    text = _LINK_TEXT.sub(r"\1", text)
    text = _HTML_TAG.sub("", text)
    text = _ESCAPED.sub(r"\1", text)
    text = _CITATION.sub("", text)
    text = _EMPHASIS.sub("", text)
    # A citation sat between the last word and its full stop; closing that gap
    # is the difference between "websites ." and "websites."
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def summarise(markdown: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> str | None:
    """The opening prose of a page, whole sentences only, within the budget.

    Returns None when the page has no prose to summarise — a product grid, a
    link directory, a nav shell. None with a warning beside it is honest;
    None on its own is the bug this module exists to close.
    """
    if not markdown or max_chars <= 0:
        return None

    kept: list[str] = []
    length = 0

    for block in _blocks(markdown):
        prose = _plain(block)
        if len(prose.split()) < MIN_PARAGRAPH_WORDS:
            continue
        if not _TERMINATED.search(prose) and len(prose.split()) < MIN_PARAGRAPH_WORDS * 2:
            # An unterminated short line is a label, not a sentence.
            continue

        for sentence in _SENTENCE_SPLIT.split(prose):
            sentence = sentence.strip()
            if not sentence:
                continue
            # +1 for the space that will join it to the sentence before.
            addition = len(sentence) + (1 if kept else 0)
            if length + addition > max_chars:
                return " ".join(kept) if kept else _first_sentence(sentence, max_chars)
            kept.append(sentence)
            length += addition

        if length >= max_chars:
            break

    return " ".join(kept) if kept else None


def _first_sentence(sentence: str, max_chars: int) -> str | None:
    """One sentence longer than the whole budget.

    Returning nothing would be wrong — the page does have prose — and cutting
    mid-word would be worse, so cut at the last word boundary that fits and
    mark it as cut.
    """
    if len(sentence) <= max_chars:
        return sentence
    cut = sentence[: max_chars - 1].rsplit(" ", 1)[0].rstrip(",;:")
    return f"{cut}…" if cut else None
