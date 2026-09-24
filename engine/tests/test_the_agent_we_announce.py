"""Tier 0 tells every site it touches who we are. That has to be true.

The shipped default was

    SnoopScan/0.1 (+https://example.invalid/bot-info)

`.invalid` is the RFC 2606 top-level domain guaranteed never to resolve. As a
placeholder that is exactly right; as the DEFAULT it meant every site we
touched on this rung was handed a dead address to complain to, and the
publish gate's "no unfilled placeholders" check passed over it because it is a
legitimate domain to write.

The version was the second half of it. `0.1` in a public User-Agent answers
only one question for a site operator — "how finished is this" — and answers
it badly. Nothing about a request depends on it, and bumping it would change
the token an operator has already written into their own robots.txt.
"""

from __future__ import annotations

import re

from engine.settings import settings

# The token before the first space is what an operator writes after
# `User-agent:` in robots.txt, and what the robots parser matches on. It is a
# published identifier: changing it silently breaks every rule written about
# us, so it is pinned here rather than left to drift.
AGENT_TOKEN = "SnoopScan"
CONTACT_URL = "https://snoopscan.com/bot"


def test_the_agent_names_itself_and_says_where_to_read_about_it() -> None:
    ua = settings.user_agent

    assert ua.split()[0] == AGENT_TOKEN, "the robots.txt token is a published identifier"
    assert f"(+{CONTACT_URL})" in ua, "the `+URL` convention: Googlebot, bingbot, CCBot"


def test_the_contact_url_is_not_a_domain_that_cannot_resolve() -> None:
    """`.invalid`, `.test`, `.example` and `.localhost` are reserved by RFC
    2606 precisely so they never resolve. One of them shipped as the default."""
    ua = settings.user_agent

    for reserved in (".invalid", ".test", ".example", ".localhost", "example.com"):
        assert reserved not in ua, f"the contact URL points at {reserved}, which cannot answer"


def test_the_agent_carries_no_version() -> None:
    """A version in a public identity is read as "how finished is this", and
    a bump would invalidate robots.txt rules written against the token."""
    token = settings.user_agent.split()[0]

    assert "/" not in token, f"{token} carries a version"
    assert not re.search(r"\d+\.\d+", settings.user_agent), settings.user_agent


def test_a_site_can_block_us_by_name_in_robots() -> None:
    """The instruction the bot page gives has to be one the parser honours.

    A named group beats the wildcard — so a site that goes out of its way to
    name us gets a stricter answer than one that blanket-disallows, which is
    the whole point of publishing the token.
    """
    from engine.core.robots import parse

    body = f"User-agent: *\nDisallow: /private\n\nUser-agent: {AGENT_TOKEN}\nDisallow: /\n"
    rules = parse(body, user_agent=settings.user_agent)

    assert rules.permits("/anything") is False, "a rule written about us must bind"

    # And the wildcard still applies to us when we are not named.
    wildcard_only = parse("User-agent: *\nDisallow: /private\n", user_agent=settings.user_agent)
    assert wildcard_only.permits("/public") is True
    assert wildcard_only.permits("/private") is False
