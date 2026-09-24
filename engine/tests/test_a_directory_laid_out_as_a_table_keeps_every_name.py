"""A register laid out as a one-column table keeps each entry's name and details.

Companies House's advanced search puts each company in one table cell: a
heading with the name, then a list. The main-content path dropped the name
(trafilatura loses headings inside cells) and the full-page path dropped the
list (a table row cannot hold one), so either way half of every lead was gone.
"""

from __future__ import annotations

from engine.core.extract.router import ExtractOptions, extract, tidy_markdown

URL = "https://register.example.gov/search/results"


def _card(name: str, number: str, street: str) -> str:
    return (
        '<tr class="row"><td class="cell">'
        f'<h2><a href="/company/{number}">{name}</a></h2>'
        '<p><span class="bold">Active</span></p>'
        "<ul><li>Private limited company</li><li></li>"
        f"<li>{number} - Incorporated on 25 June 2019</li>"
        f"<li>{street}, Manchester M29 8DG</li><li>SIC codes - 43910</li></ul>"
        "</td></tr>"
    )


PAGE = (
    "<html><head><title>Advanced company search</title></head><body><main>"
    "<h1>Advanced company search</h1><p>Fill in one or more fields to search the register.</p>"
    "<table><tbody>"
    + _card("KENNY ROOFING SOLUTIONS LIMITED", "12069139", "231 Elliott Street")
    + _card("FIRST CHOICE ROOFING JOINERS LTD", "15732010", "12 Calcot Walk")
    + _card("NUTSFORD VALE ROOFING LTD", "15733385", "71 Haworth Road")
    + _card("APPROVED ROOFING LTD", "07339946", "161 Manchester Road")
    + _card("PLASTIC PLUS LTD", "15730651", "Bury Road")
    + "</tbody></table></main></body></html>"
)


def test_every_entry_keeps_its_name_and_its_details_on_either_path() -> None:
    for main in (True, False):
        md = extract(PAGE, URL, ExtractOptions(only_main_content=main)).markdown
        for name, number, street in (
            ("KENNY ROOFING SOLUTIONS LIMITED", "12069139", "231 Elliott Street"),
            ("FIRST CHOICE ROOFING JOINERS LTD", "15732010", "12 Calcot Walk"),
            ("NUTSFORD VALE ROOFING LTD", "15733385", "71 Haworth Road"),
        ):
            assert name in md, f"name lost (only_main_content={main})"
            assert number in md and street in md, f"details lost (only_main_content={main})"
        assert "https://register.example.gov/company/12069139" in md
        # The same value on every entry is each entry's own field, not a
        # repeated artefact: all five are Active, private and SIC 43910.
        for field in ("Active", "Private limited company", "SIC codes - 43910"):
            assert md.count(field) == 5, f"{field} collapsed (only_main_content={main})"


def test_a_real_data_table_is_left_a_table() -> None:
    page = (
        "<html><body><main><table><tr><th>Plan</th><th>Price</th></tr>"
        "<tr><td>Basic</td><td>$49</td></tr><tr><td>Pro</td><td>$79</td></tr>"
        "</table></main></body></html>"
    )
    md = extract(page, URL, ExtractOptions(only_main_content=False)).markdown
    assert "| Basic | $49 |" in md


def test_links_resolve_and_empty_bullets_go() -> None:
    md = tidy_markdown(
        "[a](/x) [b](../y) ![i](img.png) [c](https://z.com/q) [d](#top) "
        "[e](mailto:a@b.c) [f](//cdn.x/y)\n- \n- real\n---\n",
        "https://s.com/dir/page",
    )
    assert "[a](https://s.com/x)" in md and "[b](https://s.com/y)" in md
    assert "![i](https://s.com/dir/img.png)" in md and "[f](https://cdn.x/y)" in md
    assert "[c](https://z.com/q)" in md and "[d](#top)" in md and "[e](mailto:a@b.c)" in md
    assert "- real" in md and "---" in md and "\n- \n" not in md


def test_a_sidebar_dropped_whole_is_not_mistaken_for_lost_names() -> None:
    """Related-story links dropped WITH their blurbs are correct precision."""
    from engine.core.extract.heuristic import orphaned_entries

    sidebar = "".join(
        f'<h3><a href="/s/{i}">Story {i}</a></h3><p>Teaser text for story number {i} here.</p>'
        for i in range(5)
    )
    article = "<article><p>The article body.</p></article>"
    html = f"<html><body>{article}<aside>{sidebar}</aside></body></html>"
    assert orphaned_entries(html, "The article body.") == 0
    kept_blurbs = "The article body. " + " ".join(
        f"Teaser text for story number {i} here." for i in range(5)
    )
    assert orphaned_entries(html, kept_blurbs) == 5


def test_an_angle_bracket_link_is_left_whole() -> None:
    target = "<https://en.wikipedia.org/wiki/Cladding_(construction)>"
    md = tidy_markdown(f"[cladding]({target})", "https://en.wikipedia.org/wiki/Roofer")
    assert md == f"[cladding]({target})"


def test_a_one_cell_table_is_layout_and_no_tag_leaks_around_a_table() -> None:
    """indeed.com wraps each job's title in a one-cell table inside a list item.
    Every job came back as a headerless table, with a literal "<p>" either side.
    """
    jobs = "".join(
        f"<li><div><table><tr><td><h3><a href='/rc/{i}'>Roofer {i}</a></h3>"
        f"<span>Company {i}</span><span>Katy, TX</span></td></tr></table>"
        f"<ul><li>Two years of commercial roofing.</li><li>Lift {40 + i} pounds.</li></ul>"
        "</div></li>"
        for i in range(6)
    )
    page = (
        f"<html><body><main><h1>roofer jobs in Houston, TX</h1><ul>{jobs}</ul></main></body></html>"
    )
    md = extract(page, "https://jobs.example.com/jobs?q=roofer", ExtractOptions()).markdown
    assert "<p>" not in md and "</p>" not in md
    assert "|  |" not in md, "a one-cell table survived as a table"
    for i in range(6):
        assert f"Roofer {i}" in md and f"Company {i}" in md


def test_a_real_table_inside_a_list_keeps_no_stray_tags() -> None:
    page = (
        "<html><body><main><ul><li>Plans<table><tr><th>Plan</th><th>Price</th></tr>"
        "<tr><td>Basic</td><td>$49</td></tr></table></li></ul></main></body></html>"
    )
    md = extract(page, URL, ExtractOptions(only_main_content=False)).markdown
    assert "<p>" not in md and "| Basic | $49 |" in md
