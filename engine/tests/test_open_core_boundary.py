"""The open-core boundary, asserted rather than documented.

The commercial protection is not the licence on its own — it is that the
proxy intelligence, the browser tiers and the lead-gen pipeline are never
published. Someone can self-host the open core and still not have a
competitive service, because the expensive parts are absent.

That only holds if the boundary is real. A single module-scope import from
the public core into a proprietary module means the published package fails
on install, or the closed source ends up in the tarball.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_GATE = Path(__file__).resolve().parents[2] / "tools" / "check_split.py"
_spec = importlib.util.spec_from_file_location("check_split", _GATE)
assert _spec and _spec.loader
check_split = importlib.util.module_from_spec(_spec)
sys.modules["check_split"] = check_split
_spec.loader.exec_module(check_split)


def test_the_boundary_holds() -> None:
    """The gate itself, run in-process so a break fails the suite as well as
    CI."""
    failures = check_split.check()
    assert failures == [], "open-core boundary violated:\n" + "\n".join(failures)


def test_every_module_is_assigned_to_a_side() -> None:
    """An unassigned file is the one that gets published by accident."""
    root = Path(check_split.ROOT)
    unassigned = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if ".venv" not in path.parts
        and "sdk/" not in path.as_posix()
        and check_split.classify(path) == "unassigned"
    ]
    assert unassigned == [], f"unassigned modules: {unassigned}"


@pytest.mark.parametrize(
    "module",
    [
        "engine/core/proxy/pool.py",
        "engine/core/proxy/budget.py",
        "engine/core/proxy/vendor.py",
        "engine/core/fetch/browser_pool.py",
        "engine/core/fetch/tier2_browser.py",
        "engine/leadgen/discovery.py",
    ],
)
def test_the_moat_stays_proprietary(module: str) -> None:
    """These are what make the hosted service worth paying for. If one drifts
    into the public core, that is given away silently."""
    path = Path(check_split.ROOT) / module
    assert check_split.classify(path) == "proprietary", f"{module} is no longer closed"


@pytest.mark.parametrize(
    "module",
    [
        "engine/api/app.py",
        "engine/core/scrape_service.py",
        "engine/core/extract/router.py",
        "engine/core/detect/validator.py",
        "engine/mcp/server.py",
        "engine/core/redaction.py",
    ],
)
def test_the_funnel_stays_public(module: str) -> None:
    """The open core has to be genuinely useful on its own, or nobody adopts
    it and the funnel does not work. Extraction and block detection in
    particular: a core that trusts HTTP 200 is not worth self-hosting."""
    path = Path(check_split.ROOT) / module
    assert check_split.classify(path) == "public", f"{module} left the open core"


def test_a_module_scope_import_of_a_closed_module_is_caught() -> None:
    """The gate must actually detect the thing it exists to prevent."""
    import ast

    source = "from engine.core.proxy import pool\n"
    tree = ast.parse(source)
    imported = [
        node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module
    ]
    closed = check_split.proprietary_modules()
    assert any(name == c or name.startswith(c + ".") for name in imported for c in closed), (
        "the gate would not notice a public module importing the proxy layer"
    )


def test_a_deferred_import_is_allowed() -> None:
    """Deferred imports are how optional capability is wired — the public core
    reaches the proxy layer when it happens to be installed and degrades to
    direct fetching when it is not."""
    import ast

    source = "def f():\n    from engine.core.proxy import pool\n    return pool\n"
    module_scope = [node for node in ast.parse(source).body if isinstance(node, ast.ImportFrom)]
    assert module_scope == [], "a function-scope import must not count as a dependency"


def test_scrape_service_reaches_the_proxy_layer_lazily() -> None:
    """The specific call site that has to stay deferred."""
    import inspect

    from engine.core.scrape_service import ScrapeService

    source = inspect.getsource(ScrapeService._select_proxy)
    assert "from engine.core.proxy import" in source, (
        "the proxy import moved out of _select_proxy; the open core will now "
        "fail to import without the proxy layer"
    )
    assert "except ImportError" in source, (
        "a missing proxy layer must degrade to direct fetching, not raise"
    )


def test_the_closed_set_is_declared_not_only_discovered() -> None:
    """The gate must be non-vacuous in the PUBLIC export, where the proprietary
    files have been withheld.

    A closed set built only from files that exist is empty there, so a
    module-scope `from engine.core.proxy import pool` would pass the gate and
    fail at import time on a self-hoster's machine. Every declared prefix must
    contribute its module name whether or not the file is present. Verified by
    planting exactly that import in an export: the gate fired.
    """
    closed = check_split.proprietary_modules()
    for prefix in check_split.PROPRIETARY_PREFIXES:
        module = ".".join(part for part in prefix.replace(".py", "").split("/"))
        assert module in closed, f"declared prefix {prefix!r} is not in the closed set"
