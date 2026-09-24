"""A plain-text file is content, not a challenge page.

Found fetching a word list for a dictionary (23 Sep 2026): a 200 `text/plain`
file of one word per line was judged `implausible_content` — no prose, no
links, no sentences — and the fetch climbed all six tiers, paying for each,
before answering BLOCKED. The plausibility layers read a page; a `.txt` file
is not one, and no bot wall serves its challenge as `text/plain`.
"""

from __future__ import annotations

from engine.core.detect.validator import ExtractionSummary, validate
from engine.core.fetch.base import FetchResult


def _txt(
    body: bytes, status: int = 200, content_type: str = "text/plain; charset=utf-8"
) -> FetchResult:
    return FetchResult(
        url="https://files.example.com/words/20k.txt",
        status_code=status,
        headers={"content-type": content_type},
        body=body,
        content_type=content_type,
        latency_ms=80,
        bytes_transferred=len(body),
        tier="http",
        error=None,
    )


WORDS = "\n".join(["the", "of", "and", "to", "a", "in", "for", "is", "on", "that"] * 200).encode()


def test_a_word_list_is_accepted_before_and_after_extraction() -> None:
    result = _txt(WORDS)
    assert validate(result).ok
    summary = ExtractionSummary(
        word_count=2000, char_count=len(WORDS), link_count=0, markdown=WORDS.decode()
    )
    verdict = validate(result, None, summary)
    assert verdict.ok, f"judged {verdict.reason}/{verdict.signal}"


def test_a_short_text_file_is_accepted() -> None:
    body = b"User-agent: *\nDisallow: /private\n"
    summary = ExtractionSummary(
        word_count=4, char_count=len(body), link_count=0, markdown=body.decode()
    )
    assert validate(_txt(body), None, summary).ok


def test_an_error_status_on_a_text_file_is_still_an_error() -> None:
    verdict = validate(_txt(b"Forbidden", status=403))
    assert not verdict.ok


def test_an_html_challenge_mislabelled_as_text_is_still_judged() -> None:
    """A body that is really HTML goes through every layer whatever its label."""
    body = (
        b"<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
        b"<body>cf-chl</body></html>"
    )
    summary = ExtractionSummary(
        word_count=3, char_count=len(body), link_count=0, markdown="Just a moment..."
    )
    assert not validate(_txt(body), None, summary).ok


def test_a_server_error_on_a_text_file_is_not_content() -> None:
    body = b"Internal Server Error"
    summary = ExtractionSummary(
        word_count=3, char_count=len(body), link_count=0, markdown=body.decode()
    )
    assert not validate(_txt(body, status=500), None, summary).ok
