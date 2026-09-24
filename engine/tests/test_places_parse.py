"""Places parsers against fixtures trimmed from live Maps renders (5 Sep 2026).

A Maps markup change must be a failing test here, not silent nulls in a
customer's list. The fixtures keep structure, ARIA and text; classes and
tracking attributes are stripped, because nothing here may depend on them.
"""

from __future__ import annotations

from pathlib import Path

from engine.places.parse import decode_place_url, parse_detail, parse_results

FIXTURES = Path(__file__).parent / "fixtures"
RESULTS = (FIXTURES / "maps_results.html").read_text()
DETAIL = (FIXTURES / "maps_place_detail.html").read_text()

JOS_URL = (
    "https://www.google.com/maps/place/Jo%27s+Coffee+%E2%80%93+South+Congress/data="
    "!4m7!3m6!1s0x8644b4fda2c12fd5:0x66b58cb4722b37b!8m2!3d30.2510458!4d-97.7493717"
    "!16s%2Fm%2F020z01l!19sChIJ1S_Bov20RIYRe7MiR8tYawY?authuser=0&hl=en&rclk=1"
)


def test_the_place_link_carries_the_identity() -> None:
    ident = decode_place_url(JOS_URL)
    assert ident.feature_id == "0x8644b4fda2c12fd5:0x66b58cb4722b37b"
    assert ident.latitude == 30.2510458
    assert ident.longitude == -97.7493717


def test_every_result_block_becomes_a_place_in_page_order() -> None:
    places = parse_results(RESULTS)
    assert len(places) == 4
    assert [p.name for p in places][:2] == [
        "Jo's Coffee – South Congress",
        "Mozart's Coffee Roasters",
    ]
    assert all(p.feature_id.startswith("0x") and ":" in p.feature_id for p in places)
    assert all(p.place_url.startswith("https://www.google.com/maps/place/") for p in places)
    assert all("?" not in p.place_url for p in places), "tracking query dropped from the stored URL"


def test_the_fields_of_the_first_place_are_exactly_what_the_page_says() -> None:
    jos = parse_results(RESULTS)[0]
    assert jos.feature_id == "0x8644b4fda2c12fd5:0x66b58cb4722b37b"
    assert jos.latitude == 30.2510458 and jos.longitude == -97.7493717
    assert jos.rating == 4.4
    assert jos.category == "Coffee shop"
    assert jos.address == "1300 S Congress Ave"
    assert jos.tagline == "Coffee & snacks in a vibrant space"
    assert jos.open_status is not None and jos.open_status.startswith("Open")
    assert jos.website is None and jos.phone is None, "the list page does not carry these"


def test_a_repeated_block_is_one_place() -> None:
    # The same four blocks twice: a feature id seen already is not a second place.
    doubled = RESULTS + RESULTS
    assert len(parse_results(doubled)) == len(parse_results(RESULTS))


def test_the_detail_panel_yields_website_phone_and_full_address() -> None:
    d = parse_detail(DETAIL)
    assert d.website == "https://www.joscoffee.example/south-congress-jos"
    assert d.phone == "+1 512-852-2300"
    assert d.address == "1300 S Congress Ave, Austin, TX 78704, United States"
    assert d.rating == 4.4
    assert d.review_count == 1890


def test_a_bare_website_label_becomes_a_url_when_there_is_no_link() -> None:
    html = '<div aria-label="Website: example.co.uk ">example.co.uk</div>'
    assert parse_detail(html).website == "https://example.co.uk"
    assert parse_detail('<div aria-label="Website: not a site">x</div>').website is None
