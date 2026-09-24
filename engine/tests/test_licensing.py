"""Licensing invariants.

Two of these protect adoption rather than code, which is why they are tests
and not a note in a README: nothing in normal development would surface a
breach, and both would be discovered by a customer's legal team rather than by
us.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def read_toml(relative: str) -> dict[str, object]:
    return tomllib.loads((ROOT / relative).read_text())


# --------------------------------------------------------------------------
# The server is AGPL
# --------------------------------------------------------------------------


def test_the_licence_file_is_the_real_agpl() -> None:
    """A paraphrased or truncated licence is not the licence."""
    text = (ROOT / "LICENSE").read_text()
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 19 November 2007" in text
    # Section 13 is the clause that makes AGPL different from GPL, and the
    # whole reason this licence was chosen.
    assert "Remote Network Interaction" in text
    assert len(text) > 30_000, "the licence text looks truncated"


def test_the_server_package_declares_agpl() -> None:
    project = read_toml("pyproject.toml")["project"]
    assert project["license"] == "AGPL-3.0-only"  # type: ignore[index]


# --------------------------------------------------------------------------
# The SDK is NOT AGPL — this one protects adoption
# --------------------------------------------------------------------------


def test_the_sdk_is_permissively_licensed() -> None:
    """An AGPL client library is an adoption blocker.

    Every application that imports it would inherit the copyleft, so a
    commercial customer cannot use it — the exact opposite of what a client
    exists to do. Clients in this space are permissive for this reason.
    """
    project = read_toml("sdk/python/pyproject.toml")["project"]
    licence = str(project["license"])  # type: ignore[index]
    assert "AGPL" not in licence.upper(), "the SDK became copyleft; customers cannot use it"
    assert licence == "MIT"


def test_the_sdk_ships_its_own_licence_file() -> None:
    text = (ROOT / "sdk" / "python" / "LICENSE").read_text()
    assert "MIT License" in text
    assert "AGPL" not in text


def test_the_sdk_has_a_distinct_distribution_name() -> None:
    """Server and client cannot both be `scraping-engine` on an index."""
    server = read_toml("pyproject.toml")["project"]["name"]  # type: ignore[index]
    client = read_toml("sdk/python/pyproject.toml")["project"]["name"]  # type: ignore[index]
    assert server != client


# --------------------------------------------------------------------------
# AGPL section 13 — the obligation a hosted service actually incurs
# --------------------------------------------------------------------------


async def test_the_source_offer_is_served_by_the_software() -> None:
    """Section 13 requires a network service to offer its Corresponding Source
    to remote users. A README does not satisfy that, and a fork would not
    update one."""
    from engine.api.routes.source import source_offer

    data = (await source_offer())["data"]
    assert data["license"] == "AGPL-3.0-only"
    assert data["source"].startswith("http")
    assert "Corresponding Source" in data["notice"]


async def test_the_source_offer_needs_no_api_key() -> None:
    """The right belongs to "all users interacting with it remotely", so
    putting the offer behind authentication would defeat it."""
    from engine.api.routes import source as source_module

    route = next(r for r in source_module.router.routes if r.path == "/source")
    dependencies = getattr(route, "dependant", None)
    names = [param.name for param in dependencies.query_params] if dependencies else []
    assert "key" not in names
    assert not getattr(route, "dependencies", [])


async def test_the_offer_disclaims_the_proprietary_modules() -> None:
    """Ambiguity about what is covered invites a claim that everything is.

    The proprietary modules are separately licensed and not derived from the
    core, so the offer says so explicitly.
    """
    from engine.api.routes.source import source_offer

    components = (await source_offer())["data"]["proprietary_components"]
    assert components["modules"]
    assert "not part of the Corresponding Source" in components["notice"]


async def test_the_offer_disclaims_EVERY_withheld_module() -> None:
    """The offer must name every path that is actually withheld.

    `assert components["modules"]` — the whole of this test's predecessor —
    passes on any non-empty list, so the served offer named four of the eleven
    withheld paths for as long as nobody read it. Meanwhile the CI gate and
    LICENSE-PROPRIETARY were pinned to each other and stayed correct, which is
    what made the drift invisible. This is the copy with the legal weight: a
    section 13 offer that fails to except a module is an offer OF that module.
    """
    from engine.api.routes.source import source_offer
    from engine.licensing import PROPRIETARY_PREFIXES

    named = set((await source_offer())["data"]["proprietary_components"]["modules"])
    for prefix in PROPRIETARY_PREFIXES:
        assert any(m.rstrip("/") == prefix for m in named), (
            f"{prefix} is withheld but the /v1/source offer does not except it"
        )
    assert len(named) == len(PROPRIETARY_PREFIXES)


def test_all_three_copies_of_the_withheld_list_are_one_list() -> None:
    """The gate, the notice and the offer must read from engine/licensing.py.

    They were three hand-kept lists. Pinning them to each other pairwise is not
    enough — that is exactly the arrangement that drifted, because the pair that
    was pinned did not include the one being served.
    """
    import importlib.util
    import sys

    from engine.licensing import PROPRIETARY_PREFIXES

    gate_path = ROOT / "tools" / "check_split.py"
    spec = importlib.util.spec_from_file_location("check_split_one", gate_path)
    assert spec and spec.loader
    gate = importlib.util.module_from_spec(spec)
    sys.modules["check_split_one"] = gate
    spec.loader.exec_module(gate)

    assert gate.PROPRIETARY_PREFIXES is PROPRIETARY_PREFIXES, "the CI gate has its own copy again"


def test_the_committed_licence_file_matches_what_the_code_renders() -> None:
    """LICENSE-PROPRIETARY is GENERATED. This is the check that it is current.

    It replaces a pair of hand-parity tests. Those compared two lists a human
    kept in step, which is the arrangement that let the served offer drift: the
    pair that was pinned did not include the copy that mattered. There is now
    one list and the licence text is a view of it, so the only thing left to
    assert is that the committed view is not stale.
    """
    from engine.licensing import render_licence

    committed = (ROOT / "LICENSE-PROPRIETARY").read_text()
    assert committed == render_licence(), (
        "LICENSE-PROPRIETARY is stale or was hand-edited — run: python tools/render_licence.py"
    )


def test_the_licence_file_names_every_withheld_path() -> None:
    """A property of the rendered text, independent of how it was rendered — so
    a bug in the renderer that dropped a row cannot pass by rendering the same
    wrong thing on both sides of the comparison above."""
    from engine.licensing import offer_paths

    committed = (ROOT / "LICENSE-PROPRIETARY").read_text()
    for path in offer_paths():
        assert path in committed, f"{path} is withheld but the licence file omits it"


def test_no_file_is_unassigned_by_the_split_gate() -> None:
    """Runs the boundary gate inside the suite, not only in CI.

    This is what catches a path DELETED from `WITHHELD`: the files under it stop
    being classified and become unassigned. Every same-source assertion still
    passes after such a deletion — one list, consistently wrong — so the
    filesystem is the only independent witness left now that the licence text is
    generated rather than hand-kept. It was CI-only, which meant `pytest` was
    green on exactly that mistake.
    """
    import importlib.util
    import sys

    gate_path = ROOT / "tools" / "check_split.py"
    spec = importlib.util.spec_from_file_location("check_split_gate", gate_path)
    assert spec and spec.loader
    gate = importlib.util.module_from_spec(spec)
    sys.modules["check_split_gate"] = gate
    spec.loader.exec_module(gate)

    failures = gate.check()
    assert not failures, "open-core boundary violated:\n  " + "\n  ".join(failures)


def test_the_gate_and_the_offer_read_the_same_object() -> None:
    """Identity, not agreement. Two lists that agree today are two lists."""
    import importlib.util
    import sys

    from engine.licensing import PROPRIETARY_PREFIXES

    gate_path = ROOT / "tools" / "check_split.py"
    spec = importlib.util.spec_from_file_location("check_split_one", gate_path)
    assert spec and spec.loader
    gate = importlib.util.module_from_spec(spec)
    sys.modules["check_split_one"] = gate
    spec.loader.exec_module(gate)

    assert gate.PROPRIETARY_PREFIXES is PROPRIETARY_PREFIXES, "the CI gate has its own copy again"


@pytest.mark.parametrize("path", ["LICENSE", "LICENSE-PROPRIETARY", "sdk/python/LICENSE"])
def test_licence_files_exist(path: str) -> None:
    assert (ROOT / path).is_file(), f"{path} is missing"


# --------------------------------------------------------------------------
# Deliberately-explicit lists: not derived, but proved complete
# --------------------------------------------------------------------------


def test_every_tier_has_a_place_on_the_escalation_ladder() -> None:
    """`TIER_ORDER` is a second list beside the `Tier` enum, on purpose.

    Deriving it from declaration order would bury the escalation sequence — the
    thing that decides what a fetch costs — in the incidental order of an enum
    body. So it stays explicit, and this asserts the two cannot diverge: a tier
    added to the enum and forgotten here would simply never be escalated to, and
    nothing else would notice.
    """
    from engine.core.models import TIER_ORDER, Tier

    assert set(TIER_ORDER) == set(Tier), (
        f"tiers missing from the ladder: {sorted(set(Tier) - set(TIER_ORDER))}; "
        f"on the ladder but not a tier: {sorted(set(TIER_ORDER) - set(Tier))}"
    )
    assert len(TIER_ORDER) == len(set(TIER_ORDER)), "a tier appears twice on the ladder"


def test_the_version_is_written_in_exactly_one_place() -> None:
    """`__version__` feeds the AGPL offer, the OpenAPI schema and /health.

    It was a literal in engine/__init__.py AND in pyproject.toml, so a release
    that bumped one and not the other made all three state a version that was
    not running — and nothing failed. It is now read from pyproject.toml, and
    this asserts no second literal has crept back.
    """
    import tomllib

    import engine

    with (ROOT / "pyproject.toml").open("rb") as fh:
        declared = tomllib.load(fh)["project"]["version"]

    assert engine.__version__ == declared
    body = (ROOT / "engine" / "__init__.py").read_text()
    assert declared not in body, (
        f"the version {declared!r} is hard-coded in engine/__init__.py again; "
        "it must be read from pyproject.toml"
    )


def test_the_sdk_version_is_written_in_exactly_one_place() -> None:
    """Same rule for the client package, which reports itself to users."""
    import tomllib

    with (ROOT / "sdk" / "python" / "pyproject.toml").open("rb") as fh:
        declared = tomllib.load(fh)["project"]["version"]

    body = (ROOT / "sdk" / "python" / "snoopscan" / "__init__.py").read_text()
    assert declared not in body, (
        f"the version {declared!r} is hard-coded in the SDK's __init__.py; "
        "it must come from its distribution metadata"
    )
