"""JSON is JSON whatever the server calls it.

Reported 8 Sep 2026: `/v1/scrape` failed with `TARGET_ERROR: status None` on
Apple's review feed while `/v1/fetch` succeeded, so a harvest had to work
around the engine to get its entire quantitative base — 5,515 reviews.

The cause was the content-type whitelist. Apple answers `text/javascript` with
a JSON body, a JSONP-era convention plenty of APIs still use, and the validator
refused it before the body was ever looked at.

That whitelist has now been wrong twice — once for feeds and vendor `+json`
types, once for this. So the fix is not only another entry: a body that parses
as JSON is accepted whatever its label, which catches the next one without
needing to know its name in advance.
"""

from __future__ import annotations

import pytest

from engine.core.detect.validator import (
    DomainStats,
    Reason,
    is_structured_data,
    looks_like_json,
    validate,
)
from engine.core.fetch.base import FetchResult

APPLE_FEED = b'{"feed":{"author":{"name":{"label":"iTunes Store"}},"entry":[]}}'


def _result(content_type: str, body: bytes = APPLE_FEED) -> FetchResult:
    return FetchResult(
        url="https://itunes.apple.com/us/rss/customerreviews/id=1/json",
        status_code=200,
        headers={},
        body=body,
        content_type=content_type,
        tier="http",
        latency_ms=40,
        bytes_transferred=len(body),
    )


@pytest.mark.parametrize(
    "content_type",
    ["text/javascript; charset=UTF-8", "application/javascript", "application/x-javascript"],
)
def test_json_under_a_javascript_label_is_accepted(content_type: str) -> None:
    verdict = validate(_result(content_type), DomainStats(domain="itunes.apple.com"))
    assert verdict.ok, f"{content_type} was refused: {verdict.reason}/{verdict.signal}"


def test_json_under_a_label_nobody_listed_is_still_accepted() -> None:
    """The point of sniffing the body: the next wrong label costs nothing."""
    verdict = validate(_result("text/vnd.something-odd"), DomainStats(domain="x.example"))
    assert verdict.ok


def test_a_genuinely_unreadable_body_is_still_refused() -> None:
    """The rule exists for a reason — an image must not escalate to a browser
    tier to be re-fetched."""
    verdict = validate(
        _result("image/png", body=b"\x89PNG\r\n\x1a\n" + b"\x00" * 200),
        DomainStats(domain="x.example"),
    )
    assert not verdict.ok
    assert verdict.reason == Reason.TARGET_ERROR
    assert verdict.signal == "unsupported_content_type"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"a":1}', True),
        (b"  \n [1,2,3]", True),
        (b"<html><body>hi</body></html>", False),
        (b'{"a": ', False),  # truncated: not valid JSON
        (b"", False),
        (None, False),
    ],
)
def test_looks_like_json(body: bytes | None, expected: bool) -> None:
    assert looks_like_json(body) is expected


def test_the_javascript_labels_count_as_structured_data() -> None:
    """Structured data skips the extraction-confidence floor — without that, a
    JSON API scored SOFT_BLOCK and raised the whole domain's tier."""
    assert is_structured_data("text/javascript; charset=UTF-8")
