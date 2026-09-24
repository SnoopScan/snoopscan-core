"""Everything we accept must do something observable.

This is the guard for a CLASS of fault, not a bug. In two days the same shape
appeared eight times:

    `screenshot`  an accepted format, documented, on the response model —
                  and nothing ever called the fetcher. Always null.
    `quality`     validated 1-100, defaulted to 80, read nowhere. Always PNG.
    `parsers`     "which document parsers may run" — read only in the cache
                  key, so switching the PDF parser off got you PDF parsing
                  and a different cache entry.
    scopes        stored on every key, four checkboxes in the dashboard,
                  enforced nowhere.
    media         no format at all; images were simply dropped.

Every one passed every test, because a thing that does nothing breaks nothing.
The tests here fail when a declared capability stops being wired up, which is
the only way this class gets caught before a customer finds it.
"""

from __future__ import annotations

import inspect
import pathlib
import typing

from engine.core.models import ScrapeData, ScrapeOptions

# The response field each simple format is supposed to fill.
FORMAT_FIELD = {
    "markdown": "markdown",
    "html": "html",
    "rawHtml": "rawHtml",
    "links": "links",
    "media": "media",
    "summary": "summary",
    "screenshot": "screenshot",
    "json": "json",
    "network": "network",
}


def _simple_formats() -> list[str]:
    from engine.core import models

    formats = typing.get_args(models.SimpleFormat)

    return [f for f in formats if isinstance(f, str)]


def _response_keys() -> set[str]:
    """What a caller actually sees, so an aliased field counts.

    `json` is declared `json_` with serialization_alias="json"; comparing
    Python attribute names reported it missing when it is wired correctly.
    """
    keys = set()
    for name, field in ScrapeData.model_fields.items():
        keys.add(str(field.serialization_alias or field.alias or name))

    return keys


def test_every_format_has_a_field_on_the_response() -> None:
    """A format with nowhere to put its answer can only ever return null —
    which is what `screenshot` did for months."""
    keys = _response_keys()
    missing = [f for f in _simple_formats() if FORMAT_FIELD.get(f) not in keys]

    assert missing == [], f"formats with no response field: {missing}"


def test_the_format_map_covers_every_format_offered() -> None:
    """If a format is added and not mapped here, this test — not a customer —
    is what notices."""
    assert set(_simple_formats()) == set(FORMAT_FIELD), (
        f"offered {sorted(_simple_formats())}, mapped {sorted(FORMAT_FIELD)}"
    )


def test_every_option_is_read_somewhere_outside_its_own_declaration() -> None:
    """An option nothing reads is a promise nothing keeps.

    `parsers` passed a weaker version of this check for months by appearing in
    the cache key, so the cache-variant builder is excluded: contributing to a
    hash is not an effect a caller can observe.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    sources = [p for p in (root / "core").rglob("*.py") if p.name not in {"models.py"}] + list(
        (root / "api").rglob("*.py")
    )

    body = ""
    for path in sources:
        text = path.read_text(encoding="utf-8", errors="replace")
        # Drop the cache-variant helpers: they hash options without acting.
        for marker in ("def _extraction_variant", "def _cache_variant"):
            if marker in text:
                start = text.index(marker)
                end = text.find("\n    def ", start + 1)
                text = text[:start] + text[end if end > 0 else len(text) :]
        body += text

    unread = [name for name in ScrapeOptions.model_fields if f".{name}" not in body]

    assert unread == [], f"options nothing acts on: {unread}"


def test_a_parsed_document_reports_the_parser_path() -> None:
    """So a caller can tell why a PDF came back with no links, and so the
    metrics can separate documents from pages."""
    from engine.core.models import ExtractionPath

    assert ExtractionPath.PARSER == "parser"


def test_scrape_service_actually_calls_the_document_parser() -> None:
    """The whole fault: a parser, a price and an option existed and nothing
    on the scrape path connected them, so every PDF URL returned
    EXTRACTION_FAILED on an engine that reads PDFs perfectly well."""
    from engine.core import scrape_service

    src = inspect.getsource(scrape_service)

    assert "_parse_document" in src
    assert "parse_bytes" in inspect.getsource(scrape_service._parse_document)


def test_the_parser_is_gated_on_the_parsers_option() -> None:
    """Behavioural, not textual.

    The first version of this test asserted `"options.parsers" in source`,
    and passed against a gate I had deliberately removed — because the
    DOCSTRING mentions the option. A test that reads a comment is the same
    fault as an option that does nothing: it looks wired and is not.
    """
    from engine.core.fetch.base import FetchResult
    from engine.core.scrape_service import _parse_document

    # A PDF that genuinely parses. A hand-written stub returned None from
    # the parser itself, so the test could not tell "the gate refused" from
    # "the parser choked" — and passed with the gate deliberately removed.
    pdf = (pathlib.Path(__file__).parent / "fixtures" / "dummy.pdf").read_bytes()
    res = FetchResult(
        url="https://example.com/a.pdf",
        status_code=200,
        headers={"content-type": "application/pdf"},
        body=pdf,
        content_type="application/pdf",
        tier="http",
        latency_ms=1,
        bytes_transferred=len(pdf),
    )

    # Switched off: the parser must not run, whatever the content type says.
    assert _parse_document(res, ScrapeOptions(parsers=[])) is None

    # Switched on: it parses, and says so.
    allowed = _parse_document(res, ScrapeOptions(parsers=["pdf"]))
    assert allowed is not None, "the parser did not run when it was allowed to"
    extraction, _summary, verdict = allowed
    assert verdict.ok
    assert "Dummy PDF" in extraction.markdown
    assert str(extraction.extraction_path) == "parser"


def test_an_html_page_is_never_sent_to_the_document_parser() -> None:
    """The extractor does far more with HTML; routing it to the parser would
    silently lose links, media and every structured signal."""
    from engine.core.fetch.base import FetchResult
    from engine.core.scrape_service import _parse_document

    html = b"<html><body><p>A real page.</p></body></html>"
    res = FetchResult(
        url="https://example.com/page",
        status_code=200,
        headers={"content-type": "text/html"},
        body=html,
        content_type="text/html; charset=utf-8",
        tier="http",
        latency_ms=1,
        bytes_transferred=len(html),
    )

    assert _parse_document(res, ScrapeOptions()) is None


# --------------------------------------------------------------------------
# The behavioural half.
#
# Everything above this line checks that a format has somewhere to PUT an
# answer. That is not the same as producing one, and the difference cost us
# two formats: `summary` and `json` were both declared, both documented, both
# offered as checkboxes in our own playground, both on the response model —
# and both returned `null` on every request ever made, because nothing on any
# path computed them. Every test above passed the whole time.
#
# So: run a real scrape through the real service and require each declared
# format to arrive filled, or to be NAMED in a warning saying why it could
# not be. Silence is the failure.
# --------------------------------------------------------------------------

from dataclasses import dataclass  # noqa: E402

from engine.core.fetch.base import FetchRequest, FetchResult  # noqa: E402
from engine.core.models import Tier  # noqa: E402
from engine.core.scrape_service import ScrapeService  # noqa: E402

_PARAGRAPHS = (
    "Harbour pilots on the Clyde still board by rope ladder, in weather that "
    "would keep most small craft alongside. The work looks unchanged from a "
    "distance and is not: the transfer is timed against a tide table that is "
    "now recalculated hourly.",
    "A pilot cutter runs out to meet the ship at the fairway buoy, matches "
    "her speed, and holds station a few metres off while the ladder comes "
    "down. Nine metres is the usual climb. Twelve is legal and unpopular.",
    "The Port Authority logged 4,180 acts of pilotage in 2024, against 3,905 "
    "the year before. Most of the increase was cruise traffic at Greenock, "
    "which arrives in a season rather than across the year.",
    "Training takes between two and four years depending on the district, and "
    "ends with an examination in ship handling that candidates sit in a "
    "simulator before they sit it on the water.",
)

PAGE = (
    "<html><head><title>Pilotage on the Clyde</title>"
    '<script type="application/ld+json">'
    '{"@context":"https://schema.org","@type":"Article",'
    '"headline":"Pilotage on the Clyde","author":"Marine Desk"}'
    "</script></head><body><article><h1>Pilotage on the Clyde</h1>"
    + "".join(f"<p>{p}</p>" for p in _PARAGRAPHS)
    + '<p>See the <a href="https://example.com/tide-tables">tide tables</a> and '
    'the <a href="https://example.com/districts">district list</a>.</p>'
    '<figure><img src="https://example.com/cutter.jpg" alt="A pilot cutter"></figure>'
    "</article></body></html>"
).encode()


@dataclass
class _Page:
    """A fetcher that also takes pictures, like the browser rung does."""

    calls: int = 0

    async def fetch(self, req: FetchRequest) -> FetchResult:
        self.calls += 1
        return FetchResult(
            url=req.url,
            status_code=200,
            headers={"content-type": "text/html"},
            body=PAGE,
            content_type="text/html",
            tier="browser",
            latency_ms=40,
            bytes_transferred=len(PAGE),
        )

    async def screenshot(
        self, req: FetchRequest, full_page: bool = False, quality: int | None = None
    ) -> str:
        return "data:image/png;base64,AAAA"

    async def healthcheck(self) -> bool:
        return True


# Every declared format, in the form a caller would send it.
EVERY_FORMAT = [
    "markdown",
    "html",
    "rawHtml",
    "links",
    "media",
    "summary",
    {"type": "screenshot"},
    {
        "type": "json",
        "schema": {"type": "object", "properties": {"headline": {"type": "string"}}},
    },
]


async def _scrape_everything() -> object:
    service = ScrapeService({Tier.BROWSER: _Page()}, persist=False)
    options = ScrapeOptions.model_validate({"formats": EVERY_FORMAT, "maxAge": 0})
    outcome = await service.scrape("https://example.com/pilotage", options)
    return outcome.data


async def test_every_declared_format_arrives_or_says_why_not() -> None:
    """The guard. A format that returns null in silence fails here.

    `changeTracking` is excluded only because it needs the store to have a
    previous capture; it is exercised in the monitor tests, and it was never
    part of this fault — it was wired from the day it was offered.
    """
    data = await _scrape_everything()
    warned = " ".join(data.warnings or [])

    empty: list[str] = []
    for spec in EVERY_FORMAT:
        name = spec if isinstance(spec, str) else spec["type"]
        value = getattr(data, "json_" if name == "json" else name, None)
        if value in (None, "", [], {}) and name not in warned:
            empty.append(name)

    assert empty == [], (
        f"formats that returned nothing and said nothing: {empty}. "
        "A null with no reason is indistinguishable from a bug, and twice it was one."
    )


async def test_the_summary_is_prose_from_the_page_not_its_markup() -> None:
    """`summary` returned null on every request for months. It must now be
    real sentences — and not the markdown scaffolding around them, which the
    first working version shipped as `<sup>[\\[1\\]](https://...)`.
    """
    data = await _scrape_everything()

    assert data.summary, "summary is the format that returned null for months"
    assert "Harbour pilots on the Clyde" in data.summary
    for scaffolding in ("](", "<sup", "![", "**"):
        assert scaffolding not in data.summary, f"markup leaked into the summary: {scaffolding}"
    assert len(data.summary) <= 600


async def test_the_json_format_reads_the_page_s_own_structured_markup() -> None:
    """`json` was accepted, priced as a normal fetch and never computed.

    Answering from JSON-LD matters beyond correctness: it is the free path.
    Off the wrong HTML source it silently disappears and every request falls
    through to a paid model call.
    """
    data = await _scrape_everything()

    assert data.json_ == {"headline": "Pilotage on the Clyde"}


def test_a_citation_does_not_become_the_first_sentence_of_the_summary() -> None:
    """The real capture that caught the first working version.

    Wikipedia's markdown carries citations as `<sup>[\\[1\\]](url)</sup>` —
    escaped brackets inside link text — and a `[^\\]]*` link pattern stops at
    the first `]` and leaves the whole construction in place. The summary
    shipped as: "...extracting data from websites.<sup>[\\[1\\]](https://...)".
    """
    from engine.core.extract.summary import summarise

    markdown = (
        "# Web scraping\n\n"
        "**Web scraping**, web harvesting, or web data extraction is "
        "[data scraping](/wiki/Data_scraping) used for extracting data from "
        "websites.<sup>[\\[1\\]](https://en.wikipedia.org#citenote-1)</sup> "
        "It is a form of copying in which specific data is gathered from the web.\n"
    )

    out = summarise(markdown) or ""

    assert out.startswith("Web scraping, web harvesting")
    assert "data scraping used for extracting data from websites." in out
    for scaffolding in ("<sup", "](", "\\[", "[1]", "**"):
        assert scaffolding not in out, f"{scaffolding!r} survived into the summary"
