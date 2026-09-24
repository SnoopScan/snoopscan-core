#!/usr/bin/env python3
"""Nothing leaves this machine until it passes what CI would, and is scrubbed.

Commits are written locally and pushed only through this gate, installed as
git's pre-push hook. It refuses the push unless:

  1. the outgoing commit messages, and the lines they add to PUBLISHED files,
     name nothing on the private denylist (suppliers, where we run, incidents);
  2. every job CI runs passes here first: lint, format, strict typing, the
     open-core boundary and the full suite.

Why both: in one day, twelve pushes failed CI because a check was not run
locally (strict typing; a test importing a package CI does not install), and a
pipe swallowed a failing suite's exit status so a red commit shipped. Each
failure is a notification someone reads. And a commit message is written
against the real measurement, which names the provider and the site.

    python3 tools/pre_push.py --install     # once per clone
    git push                                # runs automatically
    python3 tools/pre_push.py --check       # the same gate, by hand

Proprietary files may name suppliers; they are never exported. The scrub reads
added lines only in files the open-core split classifies as public.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DENYLIST = ROOT / "docs" / "internal" / "publish-denylist.txt"
PY = str(ROOT / ".venv" / "bin" / "python")
ZERO = "0" * 40

GATES: list[tuple[str, list[str]]] = [
    ("lint", [PY, "-m", "ruff", "check", "engine/", "tools/"]),
    ("format", [PY, "-m", "ruff", "format", "--check", "engine/", "tools/"]),
    (
        "typecheck",
        [
            str(ROOT / ".venv" / "bin" / "mypy"),
            "--strict",
            "engine/core",
            "engine/api",
            "engine/workers",
            "engine/logging_config.py",
        ],
    ),
    ("boundary", [PY, "tools/check_split.py"]),
    ("test", [PY, "-m", "pytest", "engine/tests", "-q", "-p", "no:cacheprovider"]),
]

HOOK = """#!/bin/sh
# Installed by tools/pre_push.py --install. Refuses a push that CI would fail
# or that carries a denylisted name. Bypass only with intent: git push --no-verify
exec "{py}" "{script}" --hook "$@"
"""


def _git(*args: str) -> str:
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *args],  # noqa: S607 - git from PATH, as every hook runs it
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout


def _patterns() -> list[re.Pattern[str]]:
    if not DENYLIST.exists():
        return []
    return [
        re.compile(line.strip(), re.IGNORECASE)
        for line in DENYLIST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _is_public(path: str) -> bool:
    sys.path.insert(0, str(ROOT))
    from tools.check_split import classify  # noqa: PLC0415 - local tool, lazy

    full = ROOT / path
    try:
        return classify(full) == "public"
    except ValueError:
        return False


def outgoing(stdin_lines: list[str]) -> list[str]:
    """Commits a push would send, from git's pre-push stdin."""
    commits: list[str] = []
    for line in stdin_lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_sha, remote_sha = parts[1], parts[3]
        if local_sha == ZERO:
            continue  # a delete sends no commits
        base = remote_sha if remote_sha != ZERO else "origin/main"
        commits += _git("rev-list", f"{base}..{local_sha}").split()
    return list(dict.fromkeys(commits))


def scrub(commits: list[str]) -> list[str]:
    patterns = _patterns()
    if not patterns:
        return ["no private denylist at docs/internal/publish-denylist.txt — refusing blind"]
    problems: list[str] = []
    for sha in commits:
        message = _git("log", "-1", "--format=%B", sha)
        for p in patterns:
            if p.search(message):
                problems.append(f"{sha[:7]} message names {p.pattern!r}")
        for path in _git("diff-tree", "--no-commit-id", "--name-only", "-r", sha).split():
            if not path.endswith((".py", ".md", ".yaml", ".yml", ".toml", ".txt", ".example")):
                continue
            if not _is_public(path):
                continue
            added = [
                ln[1:]
                for ln in _git("show", "--format=", "-U0", sha, "--", path).splitlines()
                if ln.startswith("+") and not ln.startswith("+++")
            ]
            for text in added:
                for p in patterns:
                    if p.search(text):
                        problems.append(f"{sha[:7]} {path} adds {p.pattern!r}: {text.strip()[:80]}")
    return problems


def run_gates() -> list[str]:
    failed: list[str] = []
    for name, cmd in GATES:
        print(f"  running {name} ...", flush=True)
        result = subprocess.run(  # noqa: S603 - our own gate commands
            cmd, cwd=ROOT, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            tail = (result.stdout + result.stderr).strip().splitlines()[-6:]
            failed.append(f"{name} failed:\n      " + "\n      ".join(tail))
    return failed


def gate(commits: list[str]) -> int:
    if not commits:
        return 0
    print(f"pre-push: {len(commits)} commit(s) outgoing")
    problems = scrub(commits)
    if problems:
        print("REFUSED — scrub before it goes live:")
        for p in problems:
            print(f"  x {p}")
        print("Reword with `git commit --amend` / `git rebase -i`, then push again.")
        return 1
    failed = run_gates()
    if failed:
        print("REFUSED — CI would fail:")
        for f in failed:
            print(f"  x {f}")
        return 1
    print("pre-push: scrubbed, and every CI job passes here. Pushing.")
    return 0


def install() -> int:
    hooks = Path(_git("rev-parse", "--git-path", "hooks").strip())
    if not hooks.is_absolute():
        hooks = ROOT / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    target = hooks / "pre-push"
    target.write_text(HOOK.format(py=PY, script=Path(__file__).resolve()), encoding="utf-8")
    target.chmod(0o755)
    print(f"installed {target}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrub and CI-check before anything is pushed")
    ap.add_argument("--install", action="store_true", help="install as git's pre-push hook")
    ap.add_argument("--hook", action="store_true", help="called by git; reads refs on stdin")
    ap.add_argument("--check", action="store_true", help="gate the commits not yet on origin")
    ap.add_argument("rest", nargs="*")
    args = ap.parse_args()
    if args.install:
        return install()
    if args.hook:
        return gate(outgoing(sys.stdin.read().splitlines()))
    return gate(_git("rev-list", "origin/main..HEAD").split())


if __name__ == "__main__":
    raise SystemExit(main())
