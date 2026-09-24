"""Layer 4 — content plausibility (05-block-detection.md section 5).

Catches generated decoy content: pages a WAF serves to suspected crawlers that
look like articles but are not. Cloudflare's AI Labyrinth does this
deliberately, so that a naive scraper ingests them without noticing.

This is the layer that matters most for this build specifically. Content flows
from here into an LLM pipeline and into a database, so a decoy that gets past
detection is not a failed fetch — it is a fabricated fact with a real URL
attached, and nobody notices until something downstream cites it.

No single signal is conclusive. They combine into a score, and the threshold
is deliberately LOW: a false positive here discards genuine content, which is
worse than missing a decoy. Every signal is recorded whether it fires or not,
so the threshold can be tuned later against real data rather than guesses.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_WORD = re.compile(r"\b[\w'-]+\b")
_SENTENCE = re.compile(r"[.!?]+\s+")
_DATE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}\s+\w+\s+\d{4}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2})\b",
    re.IGNORECASE,
)
_PRICE = re.compile(r"[$£€¥]\s?\d|(?:\d+[.,]\d{2})\s?(?:usd|gbp|eur)", re.IGNORECASE)
_TIME_REF = re.compile(
    r"\b(\d{1,2}:\d{2}|\d+\s+(?:minute|hour|day|week|month|year)s?\s+ago)\b", re.IGNORECASE
)

# Below this there is not enough text for any of these signals to mean
# anything. A short page is not a suspicious page.
MIN_WORDS_FOR_ANALYSIS = 120

# Type-token ratio outside this band is odd in either direction: generated
# filler clusters in a narrow range, and near-total uniqueness suggests word
# salad rather than prose.
TTR_LOW = 0.18
TTR_HIGH = 0.92

# Real prose varies. Sentence lengths all within a hair of each other is a
# generator's signature, not a writer's.
MONOTONY_CV = 0.22

# How many signals must fire before content is refused. Two, not one, because
# any single signal has legitimate exceptions.
IMPLAUSIBLE_THRESHOLD = 2

# Slugs whose words are NOT expected in the body. An About page rarely says
# "about", a Contact page rarely says "contact us" in prose, a Quote form says
# "project" and "budget". Topic drift is a decoy signal for content pages —
# a URL that promised one subject and delivered another — and firing it on
# utility pages made a marketing about-page a soft block (5 Sep 2026).
_UTILITY_SLUGS = frozenset(
    {
        "about",
        "about-us",
        "aboutus",
        "team",
        "our-team",
        "contact",
        "contact-us",
        "quote",
        "get-a-quote",
        "careers",
        "jobs",
        "faq",
        "faqs",
        "pricing",
        "plans",
        "privacy",
        "privacy-policy",
        "terms",
        "terms-of-service",
        "legal",
        "cookies",
        "login",
        "signin",
        "sign-in",
        "signup",
        "sign-up",
        "register",
        "home",
        "index",
        "services",
        "work",
        "portfolio",
        "testimonials",
        "reviews",
        "partners",
    }
)


@dataclass
class PlausibilitySignals:
    """Every check, recorded whether it fired or not.

    Near-miss data is what allows the threshold to be tuned later without
    re-crawling, so the negative results are as useful as the positive ones.
    """

    analysed: bool = False
    fired: list[str] = field(default_factory=list)
    measurements: dict[str, float] = field(default_factory=dict)

    @property
    def implausible(self) -> bool:
        return self.analysed and len(self.fired) >= IMPLAUSIBLE_THRESHOLD

    @property
    def confidence(self) -> float:
        """Scales with how many independent signals agree."""
        if not self.analysed or not self.fired:
            return 0.0
        return min(0.5 + 0.15 * len(self.fired), 0.95)

    def as_details(self) -> dict[str, object]:
        return {"fired": list(self.fired), "measurements": dict(self.measurements)}


def type_token_ratio(text: str) -> float:
    words = [w.lower() for w in _WORD.findall(text)]
    return len(set(words)) / len(words) if words else 0.0


def sentence_length_variation(text: str) -> float:
    """Coefficient of variation across sentence lengths.

    Low variation means every sentence is about the same length, which real
    writing is not.
    """
    lengths = [
        len(_WORD.findall(sentence))
        for sentence in _SENTENCE.split(text)
        if len(_WORD.findall(sentence)) > 2
    ]
    if len(lengths) < 5:
        return 1.0
    mean = statistics.fmean(lengths)
    if mean == 0:
        return 1.0
    return statistics.pstdev(lengths) / mean


# Path segments that describe a site's structure rather than a page's topic.
# Without this, /posts/12345 leaves "posts" as the only slug word, and any
# page not literally containing that word is scored as topic drift — a false
# positive on an extremely common URL shape.
_STRUCTURAL_SEGMENTS = frozenset(
    {
        "post",
        "posts",
        "page",
        "pages",
        "article",
        "articles",
        "blog",
        "blogs",
        "news",
        "story",
        "stories",
        "item",
        "items",
        "entry",
        "entries",
        "index",
        "view",
        "read",
        "detail",
        "details",
        "content",
        "docs",
        "doc",
        "product",
        "products",
        "shop",
        "store",
        "category",
        "categories",
        "tag",
        "tags",
        "topic",
        "topics",
        "thread",
        "threads",
        "forum",
        "html",
        "htm",
        "php",
        "aspx",
        "amp",
        "www",
        "en",
        "gb",
        "us",
    }
)


def slug_overlap(url: str, text: str) -> float:
    """How much of the URL slug appears in the content.

    The cheap version of topic drift from the spec: a page whose content bears
    no relation to the path that led there is a substitution, not the article
    that was linked.

    Returns 1.0 when the path carries no topical words — a URL with nothing to
    compare against is not evidence of anything.
    """
    path = urlsplit(url).path
    slug_words = {
        word.lower()
        for word in re.split(r"[-_/.]", path)
        if len(word) > 3 and not word.isdigit() and word.lower() not in _STRUCTURAL_SEGMENTS
    }
    if not slug_words:
        return 1.0

    content_words = {w.lower() for w in _WORD.findall(text[:4000])}
    return len(slug_words & content_words) / len(slug_words)


def assess(
    *,
    url: str,
    markdown: str,
    page_type: str = "unknown",
    link_count: int = 0,
    external_link_count: int = 0,
    has_images: bool = False,
    has_author: bool = False,
) -> PlausibilitySignals:
    """Combine the signals. Returns what fired and what was measured."""
    signals = PlausibilitySignals()
    words = _WORD.findall(markdown)

    if len(words) < MIN_WORDS_FOR_ANALYSIS:
        # Too little text to judge. Explicitly NOT implausible — a short page
        # is not a suspicious one, and treating it as such would discard a
        # great deal of legitimate content.
        signals.measurements["word_count"] = float(len(words))
        return signals

    signals.analysed = True

    # 1. Lexical diversity. Generated filler sits in a narrow band.
    ttr = type_token_ratio(markdown)
    signals.measurements["type_token_ratio"] = round(ttr, 4)
    # A catalogue says "Add to cart" forty times and means it; low diversity is
    # the shape of a product grid, not the signature of generated filler.
    if (ttr < TTR_LOW or ttr > TTR_HIGH) and page_type not in ("product", "listing"):
        signals.fired.append("lexical_diversity")

    # 2. Structural monotony. Real prose varies in sentence length.
    variation = sentence_length_variation(markdown)
    signals.measurements["sentence_variation"] = round(variation, 4)
    if variation < MONOTONY_CV:
        signals.fired.append("structural_monotony")

    # 3. Incidental markers. Decoys lack the ordinary furniture of a real
    # page: outbound links, images, dates, an author.
    markers = sum(
        (
            external_link_count > 0,
            has_images,
            bool(_DATE.search(markdown)),
            has_author,
        )
    )
    signals.measurements["incidental_markers"] = float(markers)
    if markers == 0 and link_count < 3:
        signals.fired.append("no_incidental_markers")

    # 4. Topic drift against the URL that led here. Only where the URL
    #    promised a subject: the homepage and utility pages promise none.
    overlap = slug_overlap(url, markdown)
    if overlap == 0.0:
        # /collections/mens against a page that says "Men's": the apostrophe
        # is the whole difference. Measured on Allbirds, 6 Sep 2026.
        overlap = slug_overlap(url, markdown.replace("'", "").replace("\u2019", ""))
    signals.measurements["slug_overlap"] = round(overlap, 4)
    slug = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].lower() if "://" in url else ""
    if overlap == 0.0 and slug and slug not in _UTILITY_SLUGS:
        signals.fired.append("topic_drift")

    # 5. Elements the page type promises. A product with no price anywhere,
    # or a forum with no timestamps, is not the page it claims to be.
    if page_type == "product" and not _PRICE.search(markdown):
        signals.fired.append("product_without_price")
    if page_type == "forum" and not _TIME_REF.search(markdown) and not _DATE.search(markdown):
        signals.fired.append("forum_without_timestamps")

    return signals
