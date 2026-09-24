"""The 'move it into scrapeOptions' hint appears only where scrapeOptions exists.

It was added because /v1/batch/scrape told callers `formats` did not exist when
it was simply one level down. Applied everywhere, it told /v1/serp callers to
move a stray `actions` into a scrapeOptions that endpoint does not have.
"""

from __future__ import annotations

from engine.api.app import _paths_taking_scrape_options, _problem

EXTRA = {
    "type": "extra_forbidden",
    "loc": ("body", "actions"),
    "msg": "Extra inputs are not permitted",
}


def test_the_hint_is_given_where_scrape_options_exist() -> None:
    assert "/v1/batch/scrape" in _paths_taking_scrape_options()
    assert "scrapeOptions" in _problem(EXTRA, "/v1/batch/scrape")["message"]


def test_no_hint_on_an_endpoint_without_scrape_options() -> None:
    assert "/v1/serp" not in _paths_taking_scrape_options()
    message = _problem(EXTRA, "/v1/serp")["message"]
    assert "scrapeOptions" not in message
    assert message == "Extra inputs are not permitted"
