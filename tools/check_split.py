#!/usr/bin/env python3
"""Open-core boundary gate. Blocking, like the licence gate.

The commercial protection is NOT the licence on its own. It is that the hard
infrastructure — the proxy intelligence, the browser tiers, the lead-gen
pipeline — is never published. A competitor can self-host the open core and
still not have a competitive service, because the expensive parts are missing.

That only holds if the boundary is real, so this asserts two things:

  1. No PUBLIC module imports a PROPRIETARY one at module scope. A single
     import makes the published package fail on `pip install`, or leaks the
     closed source into the tarball.
  2. Every module is on exactly one side of the line. A file nobody assigned
     is the one that gets published by accident.

Deferred imports inside a function are allowed and are how optional
capability is wired: the public core degrades to direct fetching and HTTP
tiers when the proprietary modules are absent.

    python tools/check_split.py            # verify
    python tools/check_split.py --list     # show the split
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The published open core. This is the funnel: it must be genuinely useful on
# its own, or nobody adopts it and the funnel does not work.
PUBLIC_PREFIXES = (
    "engine/api",
    "engine/core/detect",  # trusting HTTP 200 would make the core untrustworthy
    "engine/core/extract",  # the demo value; also what people compare on
    "engine/core/frontier",
    "engine/core/fetch/base.py",
    "engine/core/fetch/tier0_http.py",
    "engine/core/fetch/tier1_impersonate.py",
    "engine/core/fetch/byte_meter.py",  # httpx/httpcore byte counting tier0 imports at module scope
    "engine/core/fetch/site_rules.py",  # consent cookies; a self-hoster hits the same walls
    "engine/core/fetch/site_rules.yaml",
    "engine/core/fetch/consent.py",  # replays "Accept all" over HTTP; same door for everyone
    "engine/core/fetch/escalation.py",
    # Clicking a tab and filling a form is ordinary browser driving; the
    # withheld knowledge is which rung clears which vendor, not this.
    "engine/core/fetch/actions.py",
    "engine/core/errors.py",
    "engine/core/models.py",
    "engine/core/urls.py",
    "engine/core/robots.py",
    "engine/core/geo.py",
    "engine/core/politeness.py",
    # A breaker keyed on one url rather than the whole host. No withheld
    # knowledge in it: it is which page is failing, not how to get past it.
    "engine/core/url_backoff.py",
    "engine/core/ssrf.py",
    # Following a redirect safely is the same public concern as ssrf.py:
    # a self-hoster fetching caller-supplied URLs needs it just as much.
    "engine/core/fetch/redirects.py",
    # Connecting to the address you validated is the same public concern:
    # a self-hoster fetching caller-supplied URLs has the same DNS gap.
    "engine/core/fetch/pinning.py",
    "engine/core/search.py",
    "engine/core/serp.py",  # bought Google results; the operator brings the credentials
    "engine/places",  # Google Maps listings; public pages, no secret in asking
    "engine/platforms",  # Shopify/WP/Substack shortcuts: the site's own public JSON
    "tools/smoke_places.py",  # exercises the now-public Places source
    "engine/core/scrape_service.py",
    "engine/core/webhooks.py",
    "engine/mcp",
    "engine/storage",
    "engine/workers",
    "engine/settings.py",
    "engine/licensing.py",  # what is withheld; the AGPL offer is served from it
    "engine/services.py",  # the three long-running services, for both supervisors
    "engine/__init__.py",
    "engine/build.py",  # which code is running; a self-hoster needs it more than we do
    "engine/core/__init__.py",
    "engine/core/fetch/__init__.py",
    "engine/data",
    "engine/core/redaction.py",
    "engine/logging_config.py",  # log safety; a self-hoster needs it as much as we do
    "engine/core/change_tracking.py",
    "engine/core/monitor.py",  # the schedule around change tracking; core product
    "engine/core/credits.py",  # metering rules; a self-hoster bills too
    "engine/core/domain_intel.py",  # RDAP and DNS; public protocols, public code
    "engine/core/parse.py",  # documents to markdown; the file half of the demo value
    "engine/core/secrets.py",  # Fernet around the env key; the registry CRUD needs it
    "engine/core/metrics.py",
    "tools/backup.py",
    "tools/backfill_link_graph.py",  # rebuilds derived data a self-hoster also has
    # Operator tooling a self-hoster genuinely needs.
    "tools/check_licences.py",
    "tools/check_split.py",
    "tools/checkpoint.py",
    "tools/extraction_score.py",
    "tools/create_key.py",
    "tools/smoke.py",
    "tools/load_test.py",
    "tools/check_publish_ready.py",
    "tools/export_public.py",  # how the public tree is cut; transparent about the split
    "tools/render_licence.py",  # renders LICENSE-PROPRIETARY from engine/licensing.py
    "tools/render_units.py",  # renders the systemd units from engine/services.py
    # "is the capability actually installed on this machine" — the question a
    # self-hoster has to answer as often as we do, and it carries no
    # secret: it reports settings as set/empty, never their values.
    "tools/parity.py",
    # The push gate. Reads the denylist from docs/internal; names nothing itself.
    "tools/pre_push.py",
    # Measures the engine against the live web; names no supplier and holds no
    # secret (it reads the key from the env file, like every other tool here).
    "tools/benchmark.py",
)

# Never published. The equivalent of a "fire engine": what makes the hosted
# service worth paying for rather than self-hosting. Defined in
# engine/licensing.py so the CI gate, LICENSE-PROPRIETARY and the /v1/source
# offer cannot drift apart — they did.
from engine.licensing import PROPRIETARY_PREFIXES  # noqa: E402

# Not shipped either way.
EXCLUDED_PREFIXES = ("engine/tests",)


def classify(path: Path) -> str:
    rel = path.relative_to(ROOT).as_posix()
    for prefix in EXCLUDED_PREFIXES:
        if rel.startswith(prefix):
            return "excluded"
    # Proprietary is checked FIRST: a file under a public directory that is
    # also named proprietary must count as proprietary, never the reverse.
    for prefix in PROPRIETARY_PREFIXES:
        if rel == prefix or rel.startswith(prefix.rstrip("/") + "/"):
            return "proprietary"
    for prefix in PUBLIC_PREFIXES:
        if rel == prefix or rel.startswith(prefix.rstrip("/") + "/"):
            return "public"
    return "unassigned"


def module_of(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def proprietary_modules() -> set[str]:
    """Every module name the public core must not import at module scope.

    Two sources, unioned. Discovered files cover anything under a proprietary
    directory. The DECLARED prefixes cover the case where the files are not
    there — which is exactly the situation in the public export, where the
    proprietary tree has been withheld. Without the declarative half the gate
    is vacuous in the public repo: nothing is found, nothing is closed, and a
    module-scope `from engine.core.proxy import pool` sails through to fail at
    import time on a self-hoster's machine instead of in CI.
    """
    out: set[str] = set()
    for prefix in PROPRIETARY_PREFIXES:
        rel = Path(prefix)
        if rel.suffix == ".py":
            rel = rel.with_suffix("")
        out.add(".".join(rel.parts))
    for path in sorted(ROOT.rglob("*.py")):
        if ".venv" in path.parts:
            continue
        if classify(path) == "proprietary":
            out.add(module_of(path))
    return out


def top_level_imports(path: Path) -> list[tuple[str, int]]:
    """Imports at MODULE scope only.

    A deferred import inside a function is how the public core reaches an
    optional capability when it happens to be installed, so those are allowed
    by design and skipped here.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []

    found: list[tuple[str, int]] = []
    for node in tree.body:  # module scope only
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.module, node.lineno))
    return found


def check() -> list[str]:
    failures: list[str] = []
    closed = proprietary_modules()

    for path in sorted(ROOT.rglob("*.py")):
        if ".venv" in path.parts or "sdk/" in path.as_posix():
            continue
        side = classify(path)
        rel = path.relative_to(ROOT).as_posix()

        if side == "unassigned":
            failures.append(
                f"{rel}: not assigned to the public core or the proprietary engine. "
                f"An unassigned file is the one that gets published by accident."
            )
            continue

        if side != "public":
            continue

        for module, line in top_level_imports(path):
            if any(module == c or module.startswith(c + ".") for c in closed):
                failures.append(
                    f"{rel}:{line}: public module imports proprietary `{module}` at "
                    f"module scope. Move it inside the function that needs it so the "
                    f"open core still runs without it."
                )
    return failures


def show() -> None:
    buckets: dict[str, list[str]] = {
        "public": [],
        "proprietary": [],
        "excluded": [],
        "unassigned": [],
    }
    for path in sorted(ROOT.rglob("*.py")):
        if ".venv" in path.parts or "sdk/" in path.as_posix():
            continue
        buckets[classify(path)].append(path.relative_to(ROOT).as_posix())

    for name in ("public", "proprietary", "unassigned"):
        print(f"\n{name.upper()} ({len(buckets[name])} files)")
        for rel in buckets[name]:
            print(f"  {rel}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Open-core boundary gate")
    parser.add_argument("--list", action="store_true", help="show the split and exit")
    args = parser.parse_args()

    if args.list:
        show()
        return 0

    failures = check()
    if failures:
        print("OPEN-CORE BOUNDARY VIOLATED:\n", file=sys.stderr)
        for failure in sorted(failures):
            print(f"  ✗ {failure}", file=sys.stderr)
        print(
            f"\n{len(failures)} problem(s). The published core must run without the "
            f"proprietary modules present.",
            file=sys.stderr,
        )
        return 1

    print("Open-core boundary intact: no public module depends on a proprietary one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
