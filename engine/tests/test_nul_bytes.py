"""A page carrying a NUL byte must not 500 after we have already fetched it.

Found on an X post (6 Sep 2026): the fetch returned 200, extraction worked, and
then storing the row raised CharacterNotInRepertoireError from Postgres, which
reached the caller as INTERNAL. The work was done and paid for; only the write
failed.
"""

from __future__ import annotations

from engine.storage.db import _scrub, _scrubbed


def test_nul_is_stripped_from_every_shape_that_reaches_a_query() -> None:
    assert _scrub("before\x00after") == "beforeafter"
    assert _scrub(["a\x00", "b"]) == ["a", "b"]
    assert _scrub(("a\x00",)) == ("a",)
    assert _scrub({"markdown": "# Hi\x00", "n": 3}) == {"markdown": "# Hi", "n": 3}
    assert _scrub({"a": [{"b": "c\x00"}]}) == {"a": [{"b": "c"}]}


def test_everything_else_passes_through_untouched() -> None:
    for value in (None, 7, 3.5, True, b"bytes", "clean text"):
        assert _scrub(value) == value

    # The common case must not copy: most strings have no NUL in them.
    text = "a perfectly ordinary page"
    assert _scrub(text) is text


def test_query_arguments_are_scrubbed_as_a_tuple() -> None:
    args = _scrubbed(("https://x.example\x00", {"title": "T\x00"}, 5))
    assert args == ("https://x.example", {"title": "T"}, 5)
    assert isinstance(args, tuple)


def test_a_nul_carrying_page_record_survives_the_round_trip() -> None:
    # The shape store_page actually writes: text columns plus a metadata dict.
    record = {
        "url": "https://example.com/post",
        "markdown": "# Title\x00\n\nBody with a stray NUL\x00 in it.",
        "raw_html": "<html>\x00</html>",
        "metadata": {"title": "Title\x00", "statusCode": 200},
    }
    cleaned = _scrub(record)

    assert "\x00" not in cleaned["markdown"]
    assert "\x00" not in cleaned["raw_html"]
    assert "\x00" not in cleaned["metadata"]["title"]
    # And nothing else moved.
    assert cleaned["metadata"]["statusCode"] == 200
    assert cleaned["markdown"].startswith("# Title")
