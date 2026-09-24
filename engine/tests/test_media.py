"""Images, video and audio the page references.

Asked for directly on 8 Sep 2026: competitors return media and we returned
none at all. A scrape of a Wikipedia article made largely of photographs came
back with zero image references, because trafilatura runs with
`include_images=False` and the link collector only reads `<a href>`.

Listing media costs nothing to run: the URL comes from the HTML, so we can go
on declining the bytes at the network layer and still hand back every URL.
"""

from __future__ import annotations

from engine.core.extract.media import collect_media
from engine.core.models import ScrapeOptions

BASE = "https://ex.test/page"


def urls(html: str) -> list[str]:
    return [m.url for m in collect_media(html, BASE)]


def test_it_resolves_relative_urls_against_the_page() -> None:
    assert urls('<img src="/a.jpg">') == ["https://ex.test/a.jpg"]


def test_it_finds_lazy_loaded_images() -> None:
    """Lazy loading is the normal case, not the exception — a page that
    lazy-loads every photograph would otherwise report none of them."""
    assert urls('<img data-src="/a.jpg">') == ["https://ex.test/a.jpg"]
    assert urls('<img data-original="/b.jpg">') == ["https://ex.test/b.jpg"]
    assert urls('<img data-lazy-src="/c.jpg">') == ["https://ex.test/c.jpg"]


def test_it_takes_the_largest_from_a_srcset() -> None:
    html = '<img srcset="/s-320.jpg 320w, /s-1280.jpg 1280w">'

    assert urls(html) == ["https://ex.test/s-1280.jpg"]


def test_it_skips_inline_and_non_http_sources() -> None:
    html = '<img src="data:image/gif;base64,R0lGOD"><img src="blob:x"><img src="/real.png">'

    assert urls(html) == ["https://ex.test/real.png"]


def test_it_separates_video_and_audio_from_images() -> None:
    html = (
        '<video><source src="/v.mp4"></video><audio><source src="/a.mp3"></audio><img src="/i.png">'
    )
    kinds = {m.type for m in collect_media(html, BASE)}

    assert kinds == {"video", "audio", "image"}


def test_a_video_poster_is_an_image_not_a_video() -> None:
    """A poster is the still frame. Typing it "video" hands a caller a JPEG
    where they asked for footage."""
    html = '<video poster="/still.jpg"><source src="/v.mp4"></video>'
    by_url = {m.url: m.type for m in collect_media(html, BASE)}

    assert by_url["https://ex.test/still.jpg"] == "image"
    assert by_url["https://ex.test/v.mp4"] == "video"


def test_it_keeps_alt_text_and_leaves_it_none_when_absent() -> None:
    items = collect_media('<img src="/a.jpg" alt="A red panda"><img src="/b.jpg">', BASE)

    assert items[0].alt == "A red panda"
    assert items[1].alt is None, "empty alt is a DECLARATION that an image is decorative"


def test_it_de_duplicates_and_keeps_document_order() -> None:
    html = '<img src="/a.jpg"><img src="/b.jpg"><img src="/a.jpg">'

    assert urls(html) == ["https://ex.test/a.jpg", "https://ex.test/b.jpg"]


def test_it_finds_nothing_in_a_page_with_no_media() -> None:
    assert collect_media("<p>Words only.</p>", BASE) == []


# --------------------------------------------------------------------------
# Mixed format lists
#
# `formats` holds bare strings AND format objects, so anything that sorts or
# compares them raises TypeError — but only with two or more entries, because
# a one-element sort never compares anything. That is why asking for markdown
# or a screenshot each worked and asking for both returned a 500, reported
# from another session's real use on 8 Sep 2026.
# --------------------------------------------------------------------------


def test_format_names_survives_a_mixed_list() -> None:
    options = ScrapeOptions.model_validate(
        {"formats": ["markdown", {"type": "screenshot", "fullPage": True}]}
    )

    assert options.format_names == ["markdown", "screenshot"]


def test_format_names_handles_every_object_format_together() -> None:
    options = ScrapeOptions.model_validate(
        {
            "formats": [
                "markdown",
                "links",
                {"type": "screenshot"},
                {"type": "json", "schema": {"type": "object"}},
            ]
        }
    )

    assert options.format_names == ["json", "links", "markdown", "screenshot"]


def test_a_single_format_was_never_the_problem() -> None:
    """The negative control: these are the two calls that always worked, and
    they must keep working."""
    assert ScrapeOptions.model_validate({"formats": ["markdown"]}).format_names == ["markdown"]
    assert ScrapeOptions.model_validate({"formats": [{"type": "screenshot"}]}).format_names == [
        "screenshot"
    ]


def test_sorting_the_raw_list_still_raises_so_the_helper_is_load_bearing() -> None:
    """Prove the helper is doing work. If `formats` ever became homogeneous
    this test fails and the helper can go."""
    import pytest

    options = ScrapeOptions.model_validate({"formats": ["markdown", {"type": "screenshot"}]})
    with pytest.raises(TypeError):
        sorted(options.formats)
