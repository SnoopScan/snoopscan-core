"""Extraction quality scoring.

Token-level F1 against a reference extraction. Precision falls when we keep
nav, cookie notices and recirculation; recall falls when we lose real content.
Both failures look identical in the output — plausible text — which is why the
number exists at all.

This is the metric 04-extraction.md asks to track as a single headline figure
in CI, and the one that answers "did that change help?" without argument.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

_TOKEN = re.compile(r"[a-z0-9£$€]+")

# Markdown syntax and structural punctuation are not content. Scoring them
# would reward emitting more syntax rather than more of the page.
_MARKDOWN_NOISE = re.compile(r"[#*_`>|\[\]()\-]+")
# A link or image TARGET is formatting, not content: resolving `/story/0` to
# `https://news.example.com/story/0` must not read as three extra words.
_LINK_TARGET = re.compile(r"\]\([^)\s]*\)")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, markdown syntax removed.

    Deliberately bag-of-words: ordering and formatting are checked by the
    structural assertions, not by this. Conflating the two makes a formatting
    change look like a content regression.
    """
    cleaned = _MARKDOWN_NOISE.sub(" ", _LINK_TARGET.sub("] ", text.lower()))
    return _TOKEN.findall(cleaned)


@dataclass
class Score:
    precision: float
    recall: float
    f1: float
    extracted_tokens: int
    reference_tokens: int
    matched_tokens: int

    def describe(self) -> str:
        return f"F1 {self.f1:.3f} (precision {self.precision:.3f}, recall {self.recall:.3f})"


def score(extracted: str, reference: str) -> Score:
    """Multiset token F1.

    Counting multiplicity matters: an extractor that repeats one paragraph
    forty times should not score as well as one that returns the page.
    """
    got = Counter(tokenize(extracted))
    want = Counter(tokenize(reference))

    got_total = sum(got.values())
    want_total = sum(want.values())
    matched = sum((got & want).values())

    if got_total == 0 or want_total == 0:
        return Score(0.0, 0.0, 0.0, got_total, want_total, 0)

    precision = matched / got_total
    recall = matched / want_total
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return Score(
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
        extracted_tokens=got_total,
        reference_tokens=want_total,
        matched_tokens=matched,
    )


def count_tables(markdown: str) -> int:
    return len(re.findall(r"(?:^\|.*\|\s*$\n?)+", markdown, re.MULTILINE))


def count_code_blocks(markdown: str) -> int:
    fences = len(re.findall(r"^```", markdown, re.MULTILINE))
    return fences // 2


def count_headings(markdown: str) -> int:
    return len(re.findall(r"^#{1,6}\s+\S", markdown, re.MULTILINE))
