"""Adjacent elements must not weld their neighbouring words into one token.

Regression for the pilot's round-3 finding (7 Sep 2026) on
thebump.com/b/kassandra-baby-name: `nameMeaning`, `historyBaby` and seven more
welded tokens. Measured against the live page, the true count was 150 — the
report saw the nine that happen to start lower-case. Downstream word-by-word
matching (`names:check-facts`) matches nothing against a welded token, so this
is silent data loss rather than a cosmetic defect.

The counterpart matters as much: markup *inside* a run of prose must NOT be
separated, or `<span>Name</span>berry` becomes two words and one corruption has
been traded for another.
"""

from __future__ import annotations

import re

import pytest
from selectolax.parser import HTMLParser

from engine.core.extract.text import node_text


def _node(html: str):
    return HTMLParser(f"<div id='r'>{html}</div>").css_first("#r")


def welded_tokens(text: str) -> list[str]:
    """Tokens containing an internal lower->upper transition.

    Deliberately case-blind about the *first* letter: an earlier version of
    this check only looked for lower-case-initial welds and reported 9 where
    there were 150.
    """
    return sorted({w for w in re.findall(r"[A-Za-z]{6,}", text) if re.search(r"[a-z][A-Z]", w)})


# The shape that actually welds on thebump: sibling elements, no whitespace
# between them, inline tags carrying block-level CSS we cannot see.
@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<a href="#">Baby name</a><a href="#">Meaning of names</a>', "Baby name Meaning of names"),
        (
            "<span>Local history</span><span>Local archive A-Z</span>",
            "Local history Local archive A-Z",
        ),
        ("<li>period</li><li>Chinese</li>", "period Chinese"),
        ("<div>gender</div><div>Contraction</div>", "gender Contraction"),
        ("<p>checklist</p><p>Birth</p>", "checklist Birth"),
        ("<table><tr><td>counter</td><td>Planner</td></tr></table>", "counter Planner"),
    ],
)
def test_sibling_elements_are_separated(html: str, expected: str) -> None:
    text = node_text(_node(html))
    assert text == expected
    assert welded_tokens(text) == []


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<span>Name</span>berry", "Nameberry"),
        ("<b>Aspen</b>'s meaning", "Aspen's meaning"),
        ("<p>The name <b>Aspen</b> means <em>tree</em>.</p>", "The name Aspen means tree."),
        ("<p>a <a href='#'>link</a> mid-sentence</p>", "a link mid-sentence"),
    ],
)
def test_markup_inside_prose_is_not_separated(html: str, expected: str) -> None:
    """The over-correction guard: a blanket separator=' ' fails every one."""
    assert node_text(_node(html)) == expected


def test_nested_containers_do_not_produce_runs_of_whitespace() -> None:
    assert node_text(_node("<div><div><p>one</p></div><div><p>two</p></div></div>")) == "one two"


def test_script_and_style_are_not_content_but_still_separate() -> None:
    node = _node("<p>before</p><script>var x='NOTCONTENT';</script><p>after</p>")
    text = node_text(node)
    assert "NOTCONTENT" not in text
    assert text == "before after"


def test_none_and_empty_are_safe() -> None:
    assert node_text(None) == ""
    assert node_text(_node("")) == ""


def test_selectolax_still_welds_so_the_helper_is_load_bearing() -> None:
    """Negative control. If selectolax ever separates these upstream, the
    helper's reason for existing has changed and someone should look at it."""
    node = _node('<a href="#">Baby name</a><a href="#">Meaning of names</a>')
    assert node.text(deep=True, strip=True) == "Baby nameMeaning of names"
