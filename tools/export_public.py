#!/usr/bin/env python3
"""Produce the public tree from the private repository.

The private repo is the source of truth: full history, proprietary modules,
internal docs, the lot. The public repo receives CURATED SNAPSHOTS of the open
core only. History never leaves, so there is nothing to scrub — and "not every
update goes public" is the default rather than something to arrange.

    python tools/export_public.py --out ../scraping-engine-public
    python tools/export_public.py --out ../scraping-engine-public --init-repo
    python tools/export_public.py --out /tmp/x --check     # gate the result

What is excluded, and why each is decided by ONE source rather than a second
list that drifts:

  * Everything under PROPRIETARY_PREFIXES — imported from check_split.py, the
    same list the boundary gate enforces. Two lists would disagree eventually.
  * Test files that import a proprietary module. Exporting them ships a red
    suite on day one, which is the worst possible first impression for a
    funnel. Detected by parsing imports, not by naming files.
  * docs/internal/ — marked "NOT for publication" in its own README.
  * Anything not tracked by git. `.env`, the venv and the database never had a
    chance.

`--check` then runs the pre-publication gate over the RESULT, not the source.
A clean private repo says nothing about a clean export.
"""

from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.check_split import PROPRIETARY_PREFIXES, classify, proprietary_modules  # noqa: E402

# Non-code paths that must not ship. Kept SHORT and each one justified; the
# code boundary lives in check_split.py and is not duplicated here.
NON_PUBLIC_PATHS: tuple[tuple[str, str], ...] = (
    ("docs/internal/", "marked NOT for publication in its own README"),
    # Operations notes: incident playbooks, proxy costs and rotation, measured
    # anti-bot findings, backup paths. The know-how the split exists to keep.
    ("docs/RUNBOOK.md", "operations runbook: incidents, costs and anti-bot findings"),
    # Documents the challenge-checkbox modules, which are themselves withheld.
    ("docs/captcha-checkbox.md", "documents the withheld challenge-widget modules"),
)


def git() -> str:
    found = shutil.which("git")
    if found is None:
        raise SystemExit("git is not on PATH")
    return found


def tracked_files() -> list[str]:
    out = subprocess.run(  # noqa: S603
        [git(), "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line]


def is_proprietary_path(rel: str) -> bool:
    return any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in PROPRIETARY_PREFIXES)


def is_non_public_path(rel: str) -> str | None:
    for prefix, reason in NON_PUBLIC_PATHS:
        if rel.startswith(prefix):
            return reason
    return None


def all_imports(path: Path) -> set[str]:
    """Every module a file imports, at ANY depth — tests import inside
    functions as often as at the top."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            # `from engine.core.fetch import tier3h_camoufox` names a public
            # PACKAGE and imports a proprietary MODULE out of it. Recording only
            # the package let a test that needs Camoufox into the export, and
            # the published suite failed to even collect.
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def imports_proprietary(path: Path, closed: set[str]) -> str | None:
    for module in all_imports(path):
        for c in closed:
            if module == c or module.startswith(c + "."):
                return module
    return None


# Tests that exercise proprietary DATA rather than importing a proprietary
# MODULE. The import walk cannot see these: they call public code whose
# BEHAVIOUR depends on engine/knowledge/signatures.yaml — the WAF signature
# database, which is withheld. Without it the validator cannot name a vendor,
# so a page that is BLOCKED in the private tree reads as THIN in the public
# one. That is correct behaviour, and the assertions are therefore untrue
# outside. They drifted red for a week because nothing ran the exported suite.
WITHHELD_TESTS: dict[str, str] = {
    "engine/tests/test_captcha_checkbox.py": "exercises the withheld challenge-widget modules",
    "engine/tests/test_bot_fight_mode.py": "asserts WAF signature detection",
    "engine/tests/test_amazon_escalate.py": "asserts WAF signature detection",
    "engine/tests/test_detection.py": "asserts WAF signature detection",
    "engine/tests/test_soft_block_escalates.py": "asserts WAF signature detection",
    "engine/tests/test_content_survey_findings.py": "asserts WAF signature detection",
    "engine/tests/test_google_interstitial.py": "asserts WAF signature detection",
    "engine/tests/test_consent.py": "asserts WAF signature detection",
    "engine/tests/test_pilot_round2.py": "asserts WAF signature detection",
    "engine/tests/test_turnstile_widget_is_not_a_challenge.py": "asserts WAF signature detection",
    # The device_verification signature lives only in signatures.yaml; the
    # public detector has no rule for it, so these assertions cannot hold.
    "engine/tests/test_device_verification_signature.py": "asserts WAF signature detection",
    "engine/tests/test_challenge_retry_matches_vendor_and_signal.py": (
        "asserts retry on a signature-named challenge"
    ),
    # NOT test_escalation.py: other tests import its helpers, so withholding
    # it breaks their collection. Its one WAF-dependent test skips instead.
    "engine/tests/test_sdk.py": "exercises platform shortcuts through the API",
    "engine/tests/test_api.py": "exercises platform shortcuts through the API",
}


def plan(files: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Split tracked files into (ship, (skip, reason))."""
    closed = proprietary_modules()
    ship: list[str] = []
    skip: list[tuple[str, str]] = []
    for rel in files:
        if is_proprietary_path(rel):
            skip.append((rel, "proprietary (check_split.PROPRIETARY_PREFIXES)"))
            continue
        reason = is_non_public_path(rel)
        if reason:
            skip.append((rel, reason))
            continue
        # The deep import walk applies to TESTS only. A public core file that
        # imports a proprietary module inside a function is not a leak — it is
        # the designed degradation path, and check_split already guarantees its
        # module-scope imports are clean. The first version of this flagged
        # deps.py, scrape_service.py and the scheduler, which would have
        # exported an engine with no fetchers.
        #
        # A test is different: it imports whatever it exercises, at any depth,
        # and if that is proprietary the test fails in the public repo.
        if rel in WITHHELD_TESTS:
            skip.append((rel, f"test depends on withheld data: {WITHHELD_TESTS[rel]}"))
            continue
        if rel.endswith(".py") and classify(ROOT / rel) == "excluded":
            offending = imports_proprietary(ROOT / rel, closed)
            if offending:
                skip.append((rel, f"test imports proprietary `{offending}`"))
                continue
        ship.append(rel)
    return ship, skip


def export(out: Path, ship: list[str]) -> None:
    if out.exists() and any(out.iterdir()):
        raise SystemExit(
            f"{out} is not empty. Refusing to write into it — an export must "
            f"start from nothing, or a stale file from last time ships by accident."
        )
    out.mkdir(parents=True, exist_ok=True)
    for rel in ship:
        src = ROOT / rel
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def init_repo(out: Path) -> None:
    """One commit. No history. That is the whole point."""
    g = git()
    subprocess.run([g, "init", "-q", "-b", "main"], cwd=out, check=True)  # noqa: S603
    subprocess.run([g, "add", "-A"], cwd=out, check=True)  # noqa: S603
    # The committer comes from the environment, not from a literal in a file
    # that is itself published. A personal address hard-coded here would ship
    # in the open-core tree and be scraped off it within the day.
    # Refused, not defaulted: without both, git falls back to this machine's
    # own identity, and the first export was about to publish a personal
    # site name and the laptop's hostname as its author (Sep 2026).
    name = os.environ.get("PUBLIC_RELEASE_NAME", "").strip()
    email = os.environ.get("PUBLIC_RELEASE_EMAIL", "").strip()
    if not name or not email:
        raise SystemExit(
            "Set PUBLIC_RELEASE_NAME and PUBLIC_RELEASE_EMAIL for the public commit's author."
        )
    author = ["-c", f"user.name={name}", "-c", f"user.email={email}"]
    subprocess.run(  # noqa: S603
        [g, *author, "commit", "-q", "-m", "Initial public release"],
        cwd=out,
        check=True,
    )


def check(out: Path) -> int:
    """Run the pre-publication gate over the EXPORT. The private repo being
    clean proves nothing about what was copied out of it."""
    gate = ROOT / "tools" / "check_publish_ready.py"
    env = {"PYTHONPATH": str(out), "PATH": subprocess.os.environ.get("PATH", "")}
    # The disclosure denylist is private (docs/internal is never exported), so
    # the gate running inside the export is told where to find it.
    denylist = ROOT / "docs" / "internal" / "publish-denylist.txt"
    if denylist.exists():
        env["PUBLISH_DENYLIST"] = str(denylist)
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(out / "tools" / "check_publish_ready.py")],
        cwd=out,
        env=env,
        check=False,
    )
    _ = gate
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the public tree")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--init-repo", action="store_true", help="git init + one commit")
    parser.add_argument("--check", action="store_true", help="run the publish gate on the result")
    parser.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    args = parser.parse_args()

    ship, skip = plan(tracked_files())

    print(f"ship {len(ship)} files, withhold {len(skip)}:\n")
    by_reason: dict[str, list[str]] = {}
    for rel, reason in skip:
        by_reason.setdefault(reason, []).append(rel)
    for reason, rels in sorted(by_reason.items()):
        print(f"  {reason}")
        for rel in rels:
            print(f"    - {rel}")
    print()

    if args.dry_run:
        return 0

    export(args.out, ship)
    print(f"exported to {args.out}")

    if args.init_repo:
        init_repo(args.out)
        print("initialised as a fresh repository with a single commit")

    if args.check:
        print("\nrunning the publish gate on the export:")
        return check(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
