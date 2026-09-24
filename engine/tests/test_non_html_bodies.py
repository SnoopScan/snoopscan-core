"""The web is not all HTML, and `/v1/scrape` is pointed at all of it.

Measured 9 Sep 2026: `https://httpbin.org/headers` returned

    {"code": "INTERNAL", "message": "An internal error occurred"}

The fetch had worked. The body was fine. `_parse_document` asked
`kind_of(url, content_type)` — the helper written for `/v1/parse`, where a
file we cannot read is a genuine error and so it RAISES — and `application/
json` is not a document. Every JSON API scrape 500'd, and with it every JSON
endpoint inside a crawl or batch. Introduced by the PDF wiring the day before;
1,556 tests passed over it, because not one of them scraped anything but HTML.

So this file scrapes the content types the web actually serves. Each one must
come back as a page or fail with a real code — never as INTERNAL, and never by
raising out of the service.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from engine.core.errors import EngineError
from engine.core.fetch.base import FetchRequest, FetchResult
from engine.core.models import ScrapeOptions, Tier
from engine.core.scrape_service import ScrapeService

_ARTICLE = (
    "Harbour pilots on the Clyde still board by rope ladder, in weather that "
    "would keep most small craft alongside. A pilot cutter runs out to meet "
    "the ship at the fairway buoy, matches her speed, and holds station a few "
    "metres off while the ladder comes down. Nine metres is the usual climb, "
    "and twelve is legal and unpopular. The Port Authority logged 4,180 acts "
    "of pilotage in 2024, against 3,905 the year before."
)

# What a request to each of these actually receives. Real shapes: an API that
# answers JSON, a feed, a plain-text file, a CSV export, a JavaScript bundle.
BODIES: dict[str, tuple[str, bytes]] = {
    "json": (
        "application/json",
        json.dumps({"headers": {"User-Agent": "snoopscan", "Accept": "*/*"}}).encode(),
    ),
    "json-vendor": (
        "application/vnd.api+json",
        json.dumps({"data": [{"id": "1", "type": "pilot"}]}).encode(),
    ),
    "rss": (
        "application/rss+xml",
        b"<?xml version='1.0'?><rss version='2.0'><channel><title>Notices"
        b"</title><item><title>Fairway buoy moved</title><description>"
        + _ARTICLE.encode()
        + b"</description></item></channel></rss>",
    ),
    "plain-text": ("text/plain; charset=utf-8", _ARTICLE.encode()),
    "csv": (
        "text/csv",
        b"year,acts\n2023,3905\n2024,4180\n",
    ),
    "javascript": (
        "text/javascript",
        b"export const tides = [1.2, 3.4, 5.6];\n",
    ),
    "xml": (
        "text/xml",
        b"<?xml version='1.0'?><notice><body>" + _ARTICLE.encode() + b"</body></notice>",
    ),
}


@dataclass
class _Serves:
    content_type: str
    body: bytes

    async def fetch(self, req: FetchRequest) -> FetchResult:
        return FetchResult(
            url=req.url,
            status_code=200,
            headers={"content-type": self.content_type},
            body=self.body,
            content_type=self.content_type,
            tier="http",
            latency_ms=20,
            bytes_transferred=len(self.body),
        )

    async def healthcheck(self) -> bool:
        return True


@pytest.mark.parametrize("name", sorted(BODIES))
async def test_a_non_html_body_never_becomes_an_internal_error(name: str) -> None:
    """Pass or refuse. `INTERNAL` means we broke, and this is how we broke."""
    content_type, body = BODIES[name]
    service = ScrapeService({Tier.HTTP: _Serves(content_type, body)}, persist=False)

    try:
        outcome = await service.scrape(
            f"https://example.com/{name}", ScrapeOptions(maxAge=0, storeInCache=False)
        )
    except EngineError:
        # A refusal is a legitimate outcome for a body with nothing in it —
        # what must never happen is an exception the API cannot name.
        return
    except Exception as exc:  # noqa: BLE001 - the whole point of the test
        pytest.fail(f"{name} ({content_type}) raised {type(exc).__name__}: {exc}")

    assert outcome.data.metadata.statusCode == 200


async def test_a_json_api_comes_back_with_its_body() -> None:
    """The case that 500'd, exactly as it was sent.

    A JSON API is a first-class thing to scrape — the crawl page mapping was
    fixed in September specifically so a JSON body fetched through a batch
    stopped arriving under the wrong field.
    """
    content_type, body = BODIES["json"]
    service = ScrapeService({Tier.HTTP: _Serves(content_type, body)}, persist=False)

    outcome = await service.scrape(
        "https://example.com/headers", ScrapeOptions(maxAge=0, storeInCache=False)
    )

    assert "snoopscan" in (outcome.data.markdown or ""), outcome.data.markdown


def test_asking_whether_something_is_a_document_does_not_raise() -> None:
    """The guard on the helper itself.

    `kind_of` raises for `/v1/parse`, where an unreadable upload IS the error.
    `document_kind` answers the same question for callers to whom "no" is an
    ordinary answer. One mapping under both; two contracts on top.
    """
    from engine.core.parse import UnsupportedDocument, document_kind, kind_of

    assert document_kind("https://example.com/a.pdf", "") == "pdf"
    assert document_kind("", "application/pdf") == "pdf"
    assert document_kind("https://example.com/api", "application/json") is None
    assert document_kind("", "") is None

    with pytest.raises(UnsupportedDocument):
        kind_of("https://example.com/api", "application/json")
