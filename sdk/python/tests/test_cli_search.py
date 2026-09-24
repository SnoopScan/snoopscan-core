"""`snoopscan search --scrape` sends a shape the API actually accepts.

`--scrape` used to add `scrapeResults: true` to the request body. The API's
SearchRequest model has no such field (extra="forbid"), so every
`search --scrape` call was rejected outright with "Extra inputs are not
permitted" — plain search and plain scrape both worked, only the combination
was broken, which is exactly the kind of gap a test per-endpoint misses.
"""

from __future__ import annotations

import argparse
from typing import Any

from snoopscan.cli import build_parser, cmd_search


class _StubClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def search(self, query: str, **options: Any) -> dict[str, Any]:
        self.calls.append({"query": query, **options})
        return self.response


def _args(*extra: str) -> argparse.Namespace:
    return build_parser().parse_args(["search", "widgets", *extra])


def test_scrape_flag_sends_scrape_options_not_scrape_results() -> None:
    client = _StubClient({"results": [], "provider": "test"})
    cmd_search(client, _args("--scrape"))
    assert len(client.calls) == 1
    body = client.calls[0]
    assert "scrapeOptions" in body
    assert "scrapeResults" not in body


def test_without_scrape_flag_no_scrape_options_are_sent() -> None:
    client = _StubClient({"results": [], "provider": "test"})
    cmd_search(client, _args())
    assert "scrapeOptions" not in client.calls[0]
