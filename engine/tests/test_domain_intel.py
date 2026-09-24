"""Off-page: what is known about a DOMAIN rather than a page.

The gap a colleague hit on 9 Sep 2026, comparing our prank pages against the
sites outranking them: "SnoopScan reads pages, not link graphs — no backlinks,
domain age or authority. Given we win every on-page signal and still lose,
off-page is the most likely real gap."

Thirty seconds of RDAP answered it. The site beating us was registered in 2000
on a ten-year registration; ours is months old.
"""

from __future__ import annotations

import pytest

from engine.core.domain_intel import normalise, parse_rdap
from engine.core.errors import InvalidRequest
from engine.storage.repositories import link_pairs

# A real RDAP response, trimmed. Hand-written shapes prove only what you
# already believe; this is the payload rdap.org returned for pranx.com.
PRANX = {
    "objectClassName": "domain",
    "ldhName": "PRANX.COM",
    "status": ["client transfer prohibited"],
    "events": [
        {"eventAction": "registration", "eventDate": "2000-01-25T05:00:00Z"},
        {"eventAction": "expiration", "eventDate": "2030-01-25T05:00:00Z"},
        {"eventAction": "last changed", "eventDate": "2025-01-12T09:03:11Z"},
    ],
    "entities": [
        {
            "roles": ["registrar"],
            "vcardArray": [
                "vcard",
                [["version", {}, "text", "4.0"], ["fn", {}, "text", "NameCheap, Inc."]],
            ],
        }
    ],
    "nameservers": [
        {"ldhName": "DNS1.P09.NSONE.NET"},
        {"ldhName": "dns2.p09.nsone.net"},
    ],
}


def test_rdap_gives_the_age_a_caller_actually_wanted() -> None:
    reg = parse_rdap(PRANX)

    assert reg.createdAt.startswith("2000-01-25")
    assert reg.expiresAt.startswith("2030-01-25")
    assert reg.registrar == "NameCheap, Inc."
    # 26 years and counting. The number is derived because every caller was
    # going to subtract those two dates themselves.
    assert reg.ageDays is not None and reg.ageDays > 9_000


def test_nameservers_are_lowercased_and_deduplicated() -> None:
    """RDAP hands them back in whatever case the registry stored, and the two
    in this real payload disagree with each other."""
    assert parse_rdap(PRANX).nameservers == ["dns1.p09.nsone.net", "dns2.p09.nsone.net"]


def test_a_registry_that_says_nothing_is_not_a_crash() -> None:
    reg = parse_rdap({})

    assert reg.createdAt is None
    assert reg.ageDays is None
    assert reg.nameservers == []


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://www.GeekPrank.com/win10-update/", "geekprank.com"),
        ("pranx.com", "pranx.com"),
        ("http://sub.example.co.uk/a?b=c", "example.co.uk"),
        ("EXAMPLE.COM.", "example.com"),
    ],
)
def test_a_domain_is_accepted_however_it_arrives(given: str, expected: str) -> None:
    assert normalise(given) == expected


@pytest.mark.parametrize("bad", ["", "   ", "not a domain", "../../etc/passwd", "localhost"])
def test_anything_that_is_not_a_domain_is_refused(bad: str) -> None:
    """The value goes into a URL PATH, so this is a boundary and not a nicety:
    `../` here would address a different RDAP endpoint entirely."""
    with pytest.raises(InvalidRequest):
        normalise(bad)


# -- the link graph ----------------------------------------------------------


def test_links_fold_to_domain_pairs_with_a_count() -> None:
    pairs = link_pairs(
        "https://pranx.com/hacker/",
        [
            "https://geekprank.com/hacker/",
            "https://geekprank.com/win10-update/",
            "https://example.org/a",
        ],
    )

    assert pairs[("pranx.com", "geekprank.com")][0] == 2
    assert pairs[("pranx.com", "example.org")][0] == 1


def test_a_site_is_not_its_own_backer() -> None:
    """Self-links are navigation. Counting them would make every site its own
    biggest referring domain, and a menu of 78 links its strongest signal."""
    pairs = link_pairs(
        "https://pranx.com/hacker/",
        ["https://pranx.com/", "https://www.pranx.com/about", "/relative", "mailto:a@b.c"],
    )

    assert pairs == {}


def test_a_page_that_is_not_a_url_contributes_nothing() -> None:
    assert link_pairs("", ["https://example.com/"]) == {}
