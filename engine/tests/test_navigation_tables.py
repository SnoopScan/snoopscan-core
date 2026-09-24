"""A menu bar built as a table is furniture, not content.

behindthename.com builds its navigation as `<table id="menubar-table">` with
nested tables. `table_to_markdown` read the nesting as "too complex for
markdown" — a rule that exists so a real data table is embedded rather than
flattened wrongly — and emitted the raw HTML. The site's menu became the page's
entire content, byte-identical for every name: 3,850 characters where the page
held 18-36 KB (/name/aspen, /name/juniper, /name/willow, /name/ivy, 7 Sep 2026).

Nothing flagged it. Every length and word-count check passed, because tags are
characters and words too.
"""

from __future__ import annotations

import pytest
from selectolax.parser import HTMLParser

from engine.core.extract.router import is_mostly_markup
from engine.core.extract.structured import _is_navigation_table, table_to_markdown

MENUBAR = """
<table id="menubar-table"><tbody><tr>
  <td><a href="/">Home</a><a href="/names">Names</a><a href="/themes">Themes</a></td>
  <td><table><tr><td><a href="/random">Random</a><a href="/submit">Submit</a></td></tr></table></td>
  <td><a href="/login">Sign In</a><a href="/help">Help</a></td>
</tr></tbody></table>
"""

DATA_TABLE = """
<table>
  <tr><th>Name</th><th>Origin</th><th>Meaning</th></tr>
  <tr><td>Sage</td><td>English</td><td>From the herb, a symbol of wisdom.</td></tr>
  <tr><td>Aspen</td><td>English</td><td>From the tree name. It became popular later.</td></tr>
</table>
"""

COMPLEX_DATA_TABLE = """
<table>
  <tr><th colspan="2">Rankings</th></tr>
  <tr><td>2024</td><td>Ranked 266 in births that year.</td></tr>
  <tr><td>2022</td><td>Peaked at 195 for girls.</td></tr>
</table>
"""


def _table(html: str):
    return HTMLParser(html).css_first("table")


def test_a_menu_bar_table_is_recognised_as_navigation() -> None:
    assert _is_navigation_table(_table(MENUBAR))


def test_a_menu_bar_table_contributes_nothing() -> None:
    assert table_to_markdown(_table(MENUBAR)) == ""


def test_a_real_data_table_is_kept() -> None:
    out = table_to_markdown(_table(DATA_TABLE))
    assert "Sage" in out and "wisdom" in out
    assert not _is_navigation_table(_table(DATA_TABLE))


def test_a_complex_data_table_renders_as_markdown_not_raw_html() -> None:
    """DECISION REVERSED 7 Sep 2026. Merged cells used to be emitted as a raw
    HTML block, on the reasoning that destroying the structure is worse than
    embedding it. Two real pages showed that trade is the wrong way round:
    hetzner.com's product matrix came back as 71 KB of `<table>` in the
    markdown field, and because tags count as words it BEAT the readable
    extraction and was handed to the caller. A colspan is now expanded across
    the columns it covers — lossy about the merge, faithful about the content,
    which is the right way round."""
    out = table_to_markdown(_table(COMPLEX_DATA_TABLE))
    assert "<table" not in out, "a markdown field must not carry raw HTML"
    assert "Rankings" in out and "Peaked at 195" in out, "the content survives"
    assert out.count("|") > 6, "it is a markdown table"


def test_a_colspan_is_expanded_across_the_columns_it_covers() -> None:
    out = table_to_markdown(_table(COMPLEX_DATA_TABLE))
    header = out.splitlines()[0]
    assert header.count("Rankings") == 2, "colspan=2 fills both columns"


def test_an_absurd_colspan_cannot_blow_up_memory() -> None:
    """The attribute is caller-controlled."""
    out = table_to_markdown(_table('<table><tr><td colspan="99999">x</td></tr></table>'))
    assert out.count("x") <= 20


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "<table id='menubar-table'><tbody><tr><td>"
            "<a href='/'>Home</a></td></tr></tbody></table>",
            True,
        ),
        ("## Meaning\n\nFrom an English surname derived from a place name.", False),
        ("A line with <em>one</em> inline tag in otherwise ordinary prose text here.", False),
        ("", False),
    ],
)
def test_markup_share_guard(text: str, expected: bool) -> None:
    """The check that would have caught this whatever the cause."""
    assert is_mostly_markup(text) is expected
