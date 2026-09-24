#!/usr/bin/env python3
"""Pre-publication gate. Run before the repository is ever made public.

Publishing is one-way. A secret pushed to a public repository is compromised
the moment it lands — rotating it afterwards is the only remedy, and rewriting
history does not help once it has been cloned or indexed. So the checks that
matter run BEFORE, not as a post-mortem.

    python tools/check_publish_ready.py

Every check is a blocking failure, not a warning. A warning on a one-way
action is a warning nobody acts on.

What this does NOT do: decide whether to publish. That is a commercial
decision. This only reports whether the repository is safe to.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files that must never have entered history, in any commit, ever.
NEVER_COMMITTED = re.compile(
    r"(^|/)\.env$|(^|/)\.env\.(?!example)|\.pem$|\.key$|\.p12$|"
    r"(^|/)id_rsa|(^|/)credentials(\.json|\.yml|\.yaml)?$"
)

# Domains a fixture is allowed to use. Constraint C3: no real identifiers.
# RFC 2606 / 6761 reserve the first four for exactly this purpose.
FIXTURE_DOMAINS = (
    "example.com",
    "example.org",
    "example.net",
    "example.edu",
    ".invalid",
    ".test",
    ".localhost",
    # RFC 2606 reserves `.example` as a TLD, not only `example.com`. Without
    # it, addresses at geo.example and northwind.example — which cannot be
    # registered by anyone — were reported as real people.
    ".example",
    "localhost",
    # Project fixture domains, deliberately not real registrations.
    "acmeworks.io",
    "example-vendor.com",
    "other.com",
    "b.io",
    "genuinecompany.io",
)

# Any domain containing this is self-evidently a fixture.
FIXTURE_MARKER = "fixture"

# Local parts that name nobody. The risk C3 guards against is committing a
# REAL individual's address — one we scraped. `someone@gmail.com` identifies no
# one, and the freemail/disposable classifier tests cannot be written without
# naming real provider domains. So the domain alone is not the signal; an
# address is only a problem when a real domain carries a name-shaped local part.
GENERIC_LOCAL_PARTS = frozenset(
    {
        "someone",
        "anyone",
        "anyone-else",
        "user",
        "test",
        "tester",
        "example",
        "foo",
        "bar",
        "baz",
        "admin",
        "info",
        "contact",
        "sales",
        "support",
        "hello",
        "noreply",
        "no-reply",
        "postmaster",
        "abuse",
        "agency",
        "founder",
        "keep",
        "mystery",
        "bounced",
        "your",
        "de",
        "fr",
        "uk",
        "a",
        "b",
        "bob",
        "alice",
        "sarah",
        "hi",
        "name",
        "real",
        "first.last",
        "first.last+tag",
    }
)

# Placeholders that mean "someone has not filled this in yet". Publishing with
# these in place ships a broken link from the README of a project whose whole
# purpose is to be a funnel.
PLACEHOLDERS = ("github.com/OWNER", "YOUR_", "CHANGEME", "xxxxx", "TODO:")

EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".ruff_cache", ".mypy_cache"}

# Files that must contain the very things this tool looks for, because they
# prove it still catches them. Excluding them is not weakening the gate — a
# checker that flags its own test fixtures reports a problem on every clean
# run, and a gate that always fails is one people learn to skip.
SELF_REFERENTIAL = {"check_publish_ready.py", "test_publish_ready.py", "publish-check.yml"}


def git() -> str:
    """Resolve git once, by absolute path.

    Same reasoning as `require_tool` in backup.py: invoking a bare name leaves
    which binary runs up to PATH, and this tool's whole job is to be trusted
    about what is in the repository.
    """
    found = shutil.which("git")
    if found is None:
        print("git is not on PATH.", file=sys.stderr)
        raise SystemExit(2)
    return found


def tracked_files() -> list[Path]:
    out = subprocess.run(  # noqa: S603
        [git(), "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        return []
    return [ROOT / line for line in out.stdout.splitlines() if line]


def readable(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def check_no_secret_file_in_history() -> list[str]:
    """A secret is compromised the moment the commit lands, not when merged."""
    out = subprocess.run(  # noqa: S603
        [git(), "log", "--all", "--diff-filter=A", "--name-only", "--pretty=format:"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return ["could not read git history — run this inside the repository"]

    offenders = sorted({p for p in out.stdout.splitlines() if p and NEVER_COMMITTED.search(p)})
    return [
        f"{p} was added in some commit. It is in history even if deleted since — "
        f"rotate the credential, do not just remove the file."
        for p in offenders
    ]


def check_env_is_ignored() -> list[str]:
    gitignore = readable(ROOT / ".gitignore")
    problems = []
    if ".env" not in gitignore:
        problems.append(".gitignore does not cover .env")
    if (ROOT / ".env").exists():
        out = subprocess.run(  # noqa: S603
            [git(), "check-ignore", ".env"], cwd=ROOT, capture_output=True, text=True, check=False
        )
        if out.returncode != 0:
            problems.append(".env exists and is NOT ignored — it would be published")
    return problems


def check_no_real_addresses(files: list[Path]) -> list[str]:
    """Constraint C3. Fixtures use reserved domains; anything else is a real
    person or company and must not be committed."""
    problems = []
    for path in files:
        if path.suffix in {".png", ".jpg", ".gz", ".ico", ".woff2"}:
            continue
        if path.name in SELF_REFERENTIAL:
            continue
        for line_no, line in enumerate(readable(path).splitlines(), 1):
            for match in EMAIL.finditer(line):
                domain = match.group(1).lower()
                local = match.group(0).split("@", 1)[0].lower()
                # Reserved domains at any TLD, including subdomains:
                # example.com, example.co.uk, sub.example.co.uk.
                if re.search(r"(^|\.)example[.\-]", domain):
                    continue
                if any(domain == d or domain.endswith(d) for d in FIXTURE_DOMAINS):
                    continue
                if FIXTURE_MARKER in domain:
                    continue
                if local in GENERIC_LOCAL_PARTS:
                    continue
                rel = path.relative_to(ROOT)
                problems.append(
                    f"{rel}:{line_no} contains {match.group(0)} — not a reserved "
                    f"fixture domain (constraint C3)"
                )
    return problems


def check_no_placeholders(files: list[Path]) -> list[str]:
    problems = []
    for path in files:
        if path.name in SELF_REFERENTIAL:
            continue
        text = readable(path)
        for placeholder in PLACEHOLDERS:
            if placeholder in text:
                rel = path.relative_to(ROOT)
                problems.append(f"{rel} still contains the placeholder {placeholder!r}")
    return problems


def _denylist() -> list[re.Pattern[str]] | None:
    """Terms the export must not contain, from a PRIVATE file.

    The list names our suppliers and where the service runs, so it cannot live
    in this file — this file is published. The exporter passes its path in
    PUBLISH_DENYLIST; run without it, the check says it was skipped rather than
    claiming a pass it did not make.
    """
    path = os.environ.get("PUBLISH_DENYLIST")
    if not path or not Path(path).exists():
        return None
    patterns = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            patterns.append(re.compile(line, re.IGNORECASE))
    return patterns


def check_no_operational_disclosure(files: list[Path]) -> list[str]:
    """No supplier names, hosting details or incident write-ups in public code.

    Engineering comments are written against real measurements, and the real
    measurement names the provider, the server's city and the site that broke.
    That is right in the private repo and wrong in the published one: it tells
    a competitor who supplies us and tells an attacker where we run.
    """
    patterns = _denylist()
    if patterns is None:
        return ["SKIPPED: no PUBLISH_DENYLIST given (run via export_public.py --check)"]
    problems = []
    for path in files:
        if path.suffix in {".png", ".jpg", ".gz", ".ico", ".woff2"}:
            continue
        for line_no, line in enumerate(readable(path).splitlines(), 1):
            for pattern in patterns:
                if pattern.search(line):
                    rel = path.relative_to(ROOT)
                    problems.append(
                        f"{rel}:{line_no} matches the private denylist ({pattern.pattern!r})"
                    )
    return problems


def check_env_example_has_no_real_values() -> list[str]:
    """`.env.example` is the one env file that IS published. A real value in it
    is a secret published deliberately."""
    example = ROOT / ".env.example"
    if not example.exists():
        return [".env.example is missing — a self-hoster has nothing to copy"]

    problems = []
    for line_no, line in enumerate(readable(example).splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        _, _, value = line.partition("=")
        value = value.strip().strip("\"'")
        if not value:
            continue
        # A template is a placeholder by construction.
        if "{" in value:
            continue
        # Anything naming a reserved domain is a documented example, not a secret.
        if "example." in value or ".invalid" in value or "localhost" in value:
            continue
        # A COMMA-separated list is a setting, not a secret. Length alone was
        # the test, and `searxng,duckduckgo` — a documented default ladder — is
        # nineteen characters, so it read as a credential. A key is opaque; a
        # list of names is not, whatever its length.
        if "," in value:
            continue
        # Nor is anything with a SPACE in it. No provider issues a key, token
        # or DSN containing whitespace — an unquoted env value with a space is
        # already broken for most parsers, which is why nobody mints one. The
        # value that forced this was our own published User-Agent,
        # `SnoopScan (+https://snoopscan.com/bot)`: a public identifier that
        # SHOULD be the real thing here, flagged as a credential because it is
        # long and does not begin with `http`. Same shape as the comma rule
        # above — a key is opaque, and a sentence is not.
        if any(ch.isspace() for ch in value):
            continue
        # A long opaque value is a real credential; a short word is a default.
        if len(value) >= 16 and not value.startswith(("http", "postgres", "redis", "<", "your")):
            problems.append(
                f".env.example:{line_no} looks like a real value, not a placeholder: "
                f"{value[:24]}..."
            )
    return problems


# Provider key formats, so a scan can tell a real one from a stand-in. A
# Stripe live secret key is 107 characters; the 38-character `sk_live_...` in
# our own gate tests is a fixture, written to prove the gate CATCHES one.
# Without this, every scan of this repo reports the same three false alarms
# and the next person reaches for the same lever we nearly reached for.
_KEY_SHAPES: tuple[tuple[str, str, int], ...] = (
    ("sk_live_", "Stripe live secret key", 100),
    ("rk_live_", "Stripe restricted key", 100),
    ("ghp_", "GitHub personal access token", 36),
    ("xoxb-", "Slack bot token", 50),
    ("AKIA", "AWS access key id", 20),
)


def looks_like_a_real_key(value: str) -> str | None:
    """The provider name when `value` is a credential of a real shape.

    Length is the discriminator that matters: a fixture is written short
    precisely so it cannot be used, and a real key cannot be short and still
    be a key. Publishable keys (`pk_live_`) are deliberately absent — they are
    designed to sit in public web pages, and ours arrive inside captured
    fixtures of OTHER people's pages, where they belong.
    """
    for prefix, name, minimum in _KEY_SHAPES:
        if value.startswith(prefix) and len(value) >= minimum:
            return name
    return None


def check_no_real_keys(files: list[Path]) -> list[str]:
    """A credential of a real shape in a file we are about to publish.

    Deliberately narrow. The broad "long opaque string" heuristic belongs to
    `.env.example`, where every value is meant to be a placeholder; applied to
    source it would flag hashes, fixtures and test vectors endlessly, and a
    check that cries wolf gets switched off.
    """
    import re

    token = re.compile(r"[A-Za-z0-9_\-]{16,}")
    problems = []
    for path in files:
        for line_no, line in enumerate(readable(path).splitlines(), 1):
            for candidate in token.findall(line):
                provider = looks_like_a_real_key(candidate)
                if provider:
                    problems.append(
                        f"{path}:{line_no} carries what looks like a real "
                        f"{provider}: {candidate[:12]}..."
                    )
    return problems


def check_licences_present() -> list[str]:
    problems = []
    for required in ("LICENSE", "README.md"):
        if not (ROOT / required).exists():
            problems.append(f"{required} is missing")
    licence = readable(ROOT / "LICENSE")
    if licence and "GNU AFFERO GENERAL PUBLIC LICENSE" not in licence:
        problems.append("LICENSE is not the AGPL text the project declares")
    return problems


def check_public_core_stands_alone() -> list[str]:
    """The open core must import and run with the proprietary modules absent.

    This is the claim the whole open-core split rests on, and the only way to
    check it is to actually remove them. Done on a COPY — never the real tree.
    """
    import os

    from tools.check_split import PROPRIETARY_PREFIXES

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "repo"
        shutil.copytree(
            ROOT,
            target,
            ignore=shutil.ignore_patterns(*SKIP_DIRS, "*.pyc"),
            symlinks=True,
        )
        for prefix in PROPRIETARY_PREFIXES:
            victim = target / prefix
            if victim.is_dir():
                shutil.rmtree(victim)
            elif victim.exists():
                victim.unlink()

        probe = (
            "import engine.api.app, engine.core.scrape_service, "
            "engine.core.extract.router, engine.workers.scheduler; print('ok')"
        )
        env = dict(os.environ, PYTHONPATH=str(target))
        out = subprocess.run(  # noqa: S603
            [sys.executable, "-c", probe],
            cwd=target,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if out.returncode != 0:
            tail = (out.stderr or "").strip().splitlines()[-3:]
            return [
                "the public core does NOT import without the proprietary modules: "
                + " / ".join(tail)
            ]
    return []


def check_public_suite_is_green() -> list[str]:
    """The exported suite must PASS, not merely import.

    `check_public_core_stands_alone` proves the core imports with the
    proprietary modules absent. That is a weaker claim than it sounds, and the
    difference cost a week: 23 tests were failing in the export while every
    gate reported green, because nothing ever RAN them. They assert WAF vendor
    detection, and the signature database is withheld — so a page that is
    BLOCKED privately reads as THIN publicly, correctly, and the assertion is
    untrue outside.

    A red suite is the worst first impression a repository can make, and it is
    invisible from the private tree by construction.
    """
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "engine/tests", "-q", "-x", "--no-header"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        # A real test failure reports on stdout; pytest not being installed
        # at all — or any other crash before pytest's own reporting starts —
        # goes to stderr instead, and fell through here as a silent empty
        # message for as long as this only looked at stdout.
        tail = (out.stdout or "").strip().splitlines()[-4:]
        if not tail:
            tail = (out.stderr or "").strip().splitlines()[-4:]
        return ["the exported test suite does NOT pass: " + " / ".join(tail)]
    return []


# --------------------------------------------------------------------------


CHECKS = (
    ("No secret file ever committed", check_no_secret_file_in_history),
    ("`.env` is ignored", check_env_is_ignored),
    ("`.env.example` holds no real values", check_env_example_has_no_real_values),
    ("Licence and README present", check_licences_present),
    ("Public core runs without proprietary modules", check_public_core_stands_alone),
    ("Exported test suite passes", check_public_suite_is_green),
)

FILE_CHECKS = (
    ("No real email addresses (C3)", check_no_real_addresses),
    ("No unfilled placeholders", check_no_placeholders),
    ("No supplier, hosting or incident disclosure", check_no_operational_disclosure),
    ("No real provider keys", check_no_real_keys),
)


def main() -> int:
    files = tracked_files()
    if not files:
        print("No tracked files found. Is this a git repository?", file=sys.stderr)
        return 2

    print(f"Pre-publication check — {len(files)} tracked files\n")
    failures: list[str] = []

    for label, check in CHECKS:
        problems = check()
        print(f"  {'PASS' if not problems else 'FAIL'}  {label}")
        failures.extend(problems)

    for label, file_check in FILE_CHECKS:
        problems = file_check(files)
        print(f"  {'PASS' if not problems else 'FAIL'}  {label}")
        failures.extend(problems)

    if failures:
        # Flush the summary before writing to stderr. Without this the two
        # streams interleave and the failure list appears ABOVE the checks it
        # refers to — which is how a truncated read of this output made two
        # problems look like one.
        sys.stdout.flush()
        print(f"\nNOT READY TO PUBLISH — {len(failures)} problem(s):\n", file=sys.stderr)
        for failure in failures[:40]:
            print(f"  x {failure}", file=sys.stderr)
        if len(failures) > 40:
            print(f"  ... and {len(failures) - 40} more", file=sys.stderr)
        return 1

    print("\nSafe to publish. This says nothing about WHETHER to — only that it is safe to.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
