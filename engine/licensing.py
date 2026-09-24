"""The one list of what is withheld from the Corresponding Source.

Four copies of this list existed and one of them drifted. The CI gate
(`tools/check_split.py`) and `LICENSE-PROPRIETARY` were pinned to each other by
a test; the third copy — the one `/v1/source` serves to users, and the only one
carrying the AGPL section 13 obligation — was pinned only by
`assert components["modules"]`, which passes on any non-empty list. It named
four of twelve paths, so the offer implicitly promised the source of the stealth
tiers, Places, the platform shortcuts and the lead pipeline (found 6 Sep 2026).

Everything now derives from `WITHHELD` below: the gate classifies against it,
`/v1/source` excepts exactly it, and LICENSE-PROPRIETARY is GENERATED from it by
`tools/render_licence.py` — no longer prose anybody has to remember to update.

A leaf module with no imports, so the CI gate can read it without importing the
engine it is checking.
"""

from __future__ import annotations

# path -> why it is withheld, in the words the licence file prints.
# Never published: the equivalent of a "fire engine", what makes the hosted
# service worth paying for rather than self-hosting.
WITHHELD: dict[str, str] = {
    "engine/authority": "authority provider recipes, observations and quota coordination",
    "tools/authority.py": "runs private authority recipes and reliability batches",
    "engine/core/proxy": "proxy selection, per-domain scoring, bandwidth budgets",
    "engine/core/fetch/browser_pool.py": "browser infrastructure",
    "engine/core/fetch/traffic.py": (
        "browser bandwidth metering and asset blocking; sits with the proxy "
        "bandwidth budgets it feeds, and only the withheld tiers call it"
    ),
    "engine/core/fetch/tier2_browser.py": "browser tier",
    "engine/core/fetch/tier3_stealth.py": "stealth tier: residential exit, fingerprint posture",
    "engine/core/fetch/tier3h_camoufox.py": "stealth-hard and mobile tiers",
    "engine/core/fetch/captcha_checkbox.py": "challenge-widget interaction",
    "engine/core/fetch/captcha_evidence.py": "challenge classification and evidence capture",
    "engine/knowledge": (
        "anti-bot knowledge: WAF body signatures and which tier clears which vendor, "
        "learned from every blocked page we have fetched"
    ),
    "engine/leadgen": "lead-generation pipeline; an internal business, not part of the product",
    "engine/leads": "Find Leads: the directory sources, merging and contact discovery",
    "tools/proxy_check.py": "exercises the proprietary proxy layer",
    "tools/smoke_ladder.py": "drives the stealth rungs against live targets",
    "tools/leadgen.py": "drives the lead pipeline; holds our directory configs",
    "tools/provider_traffic.py": "reconciles our bandwidth ledger against a supplier's usage API",
    "tools/proxy_bakeoff.py": "benchmarks proxy suppliers against each other on our targets",
}

PROPRIETARY_PREFIXES: tuple[str, ...] = tuple(WITHHELD)


def offer_paths() -> tuple[str, ...]:
    """The same paths as directory-style strings, for the public source offer.

    A reader of `/v1/source` is being told which trees are absent, so a
    directory reads better with its trailing slash; a file is left alone.
    """
    return tuple(p if p.endswith(".py") else f"{p}/" for p in PROPRIETARY_PREFIXES)


LICENCE_HEADER = """Proprietary licence — NOT covered by the AGPL

Copyright (c) 2026. All rights reserved.

The modules listed below are proprietary. They are NOT licensed under the GNU
Affero General Public License that covers the rest of this repository, and no
permission is granted to use, copy, modify or distribute them.
"""

LICENCE_FOOTER = """
These are excluded from the public distribution. The authoritative list is
engine/licensing.py, enforced in CI by tools/check_split.py: the open core must
build, import and run with every file above absent.

If you have received a copy of these files, you are not licensed to use them.

This file is GENERATED from engine/licensing.py — run tools/render_licence.py
after changing that list. Do not edit it by hand.
"""


def render_licence() -> str:
    """LICENSE-PROPRIETARY, built from WITHHELD. One list, one file."""
    width = max(len(p) for p in offer_paths()) + 2
    lines = [
        f"  {path:<{width}}{WITHHELD[key]}"
        for key, path in zip(PROPRIETARY_PREFIXES, offer_paths(), strict=True)
    ]
    return f"{LICENCE_HEADER}\n" + "\n".join(lines) + "\n" + LICENCE_FOOTER
