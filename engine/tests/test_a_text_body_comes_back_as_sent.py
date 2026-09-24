"""JSON, plain text and markdown come back exactly as the server sent them.

They went through the HTML-to-markdown converter, which escapes backslashes,
asterisks and underscores. Wiktionary's API answered with `\\"` inside its
JSON and we returned `\\\\"`, so the JSON no longer parsed and the dictionary
grounding run dropped real words as "no English entry" (23 Sep 2026).
"""

from __future__ import annotations

import json

from engine.core.detect.validator import DomainStats
from engine.core.fetch.base import FetchResult
from engine.core.models import ExtractionPath
from engine.core.scrape_service import _verbatim_body


def _res(body: bytes, content_type: str | None) -> FetchResult:
    return FetchResult(
        url="https://api.example.com/w/api.php?titles=eat",
        status_code=200,
        headers={"content-type": content_type} if content_type else {},
        body=body,
        content_type=content_type,
        latency_ms=50,
        bytes_transferred=len(body),
        tier="http",
        error=None,
    )


QUOTED = json.dumps(
    {"query": {"pages": {"1": {"extract": 'A. A. Gill, "Diary" \\ snake_case *bold*'}}}}
).encode()


def test_json_with_escaped_quotes_still_parses() -> None:
    out = _verbatim_body(_res(QUOTED, "application/json; charset=utf-8"), DomainStats())
    assert out is not None
    extraction, summary, verdict = out
    assert extraction.markdown == QUOTED.decode()
    assert json.loads(extraction.markdown)["query"]["pages"]["1"]["extract"].startswith(
        "A. A. Gill"
    )
    assert extraction.extraction_path == ExtractionPath.VERBATIM
    assert verdict.ok


def test_json_under_a_text_label_is_still_returned_verbatim() -> None:
    out = _verbatim_body(_res(QUOTED, "text/plain"), DomainStats())
    assert out is not None and out[0].markdown == QUOTED.decode()


def test_a_word_list_keeps_its_underscores_and_asterisks() -> None:
    body = b"snake_case\n*star*\nplain\n"
    out = _verbatim_body(_res(body, "text/plain"), DomainStats())
    assert out is not None and out[0].markdown == body.decode()


def test_markdown_is_returned_as_markdown() -> None:
    body = b"# llms.txt\n\n- [Docs](https://example.com/docs): the_docs\n"
    out = _verbatim_body(_res(body, "text/markdown"), DomainStats())
    assert out is not None and out[0].markdown == body.decode()


def test_a_page_still_takes_the_page_path() -> None:
    assert (
        _verbatim_body(_res(b"<html><body><p>Hi</p></body></html>", "text/html"), DomainStats())
        is None
    )
    assert (
        _verbatim_body(_res(b"<!DOCTYPE html><html><p>x</p></html>", "text/plain"), DomainStats())
        is None
    )
