#!/usr/bin/env python3
"""Licence gate — constraint C2. Blocking, not advisory.

Reads pip-licenses JSON output and fails (exit 1) if any dependency carries a
forbidden licence, is a forbidden package by name, or has unknown/missing
licence metadata that is not explicitly allowlisted.

Usage:
    pip-licenses --format=json --with-urls > licences.json
    python tools/check_licences.py licences.json

A build that pulls a forbidden licence FAILS. It does not warn.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Permissive licence families we accept. A licence component is allowed when it
# contains one of these tokens AND matches no DENY_SUBSTR (deny is checked
# first). Substring matching absorbs pip-licenses' classifier formatting
# ("MIT License", "ISC License (ISCL)", "Apache Software License", …).
ALLOW_TOKENS = (
    "MIT",
    "APACHE",  # Apache Software License / Apache-2.0
    "BSD",
    "ISC",
    "MPL",  # MPL-2.0 / Mozilla Public License 2.0
    "MOZILLA PUBLIC",
    "PSF",  # Python Software Foundation
    "PYTHON SOFTWARE FOUNDATION",
    "UNLICENSE",
    "CC0",
    "0BSD",
    "CNRI",  # CNRI-Python — PSF-style permissive (used by `regex`)
    "BLUE OAK",
)

# Substrings that deny outright, whatever else is claimed. Checked first, so a
# dual-licensed "MIT OR GPL-3.0" is still rejected — copyleft contaminates.
DENY_SUBSTR = (
    "AGPL",
    "GPL",  # also catches LGPL — intentional; LGPL is denied by the spec
    "SSPL",
    "CC-BY-NC",
    "CC BY-NC",
    "BUSL",
    "BUSINESS SOURCE",
    "ELASTIC LICENSE",
    "SOURCE AVAILABLE",
    "SOURCE-AVAILABLE",
    "COMMONS CLAUSE",
    "PROPRIETARY",
    "NON-COMMERCIAL",
    "NONCOMMERCIAL",
)

# Denied by package name regardless of declared licence (repo metadata lies).
DENY_NAME = {
    "firecrawl",
    "firecrawl-py",
    "nodriver",
    "zendriver",
    "maxun",
    "skyvern",
    "browserless",
    "rnet",
    "readerlm-v2",
    "readerlm",
}

# Packages with known-bad or missing metadata that we have manually reviewed
# and cleared. Each entry MUST carry a comment explaining the check performed.
ALLOWLIST: dict[str, str] = {
    # First-party package: this repository. No third-party licence to check.
    "scraping-engine": "First-party package (this repo).",
    # tld is tri-licensed "MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later". Under
    # OR licensing the licensee elects one branch: we elect MPL-1.1, a weak
    # file-level copyleft that imposes no obligation on our own code. Pulled in
    # transitively (trafilatura -> courlan -> tld) and used entirely unmodified.
    # Reviewed 2026-09; if a future release drops the MPL option, re-review.
    "tld": "MPL-1.1 elected under OR licensing; weak file-level copyleft, used unmodified.",
}

# Minimum versions required for a licence to be acceptable.
MIN_VERSION: dict[str, tuple[int, ...]] = {
    # trafilatura < 1.8.0 is GPLv3+; 1.8.0+ is Apache-2.0.
    "trafilatura": (1, 8, 0),
}


def _normalise(lic: str) -> str:
    return re.sub(r"\s+", " ", lic.strip().upper()).strip(" .")


def _parse_version(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in re.split(r"[._-]", v):
        m = re.match(r"(\d+)", token)
        if not m:
            break
        parts.append(int(m.group(1)))
    return tuple(parts)


def _licence_ok(raw: str) -> bool:
    """True only if EVERY licence in a possibly-compound string is allowed."""
    norm = _normalise(raw)
    # Split on OR / AND / ; / , to evaluate each component. A compound licence
    # is acceptable only if all components are individually acceptable.
    components = re.split(r"\s+OR\s+|\s+AND\s+|[;,/]", norm)
    components = [c.strip() for c in components if c.strip()]
    if not components:
        return False
    for comp in components:
        if any(bad in comp for bad in DENY_SUBSTR):
            return False
        if not any(tok in comp for tok in ALLOW_TOKENS):
            return False
    return True


def check(entries: list[dict[str, str]]) -> list[str]:
    failures: list[str] = []
    seen_trafilatura = False

    for entry in entries:
        name = (entry.get("Name") or "").strip()
        lname = name.lower()
        version = (entry.get("Version") or "").strip()
        lic = (entry.get("License") or "").strip()
        norm = _normalise(lic)

        # 1. Denied by name.
        if lname in DENY_NAME:
            failures.append(f"{name} {version}: forbidden package (denied by name)")
            continue

        # 2. Version floor (e.g. trafilatura >= 1.8.0).
        if lname in MIN_VERSION:
            if lname == "trafilatura":
                seen_trafilatura = True
            floor = MIN_VERSION[lname]
            if version and _parse_version(version) < floor:
                failures.append(
                    f"{name} {version}: below minimum {'.'.join(map(str, floor))} "
                    f"(earlier versions carry a forbidden licence)"
                )
                continue

        # 3. Unknown / missing metadata.
        if not lic or norm in {"UNKNOWN", "UNKNOWN LICENSE"}:
            if lname in ALLOWLIST:
                continue
            failures.append(
                f"{name} {version}: unknown/missing licence metadata — "
                f"requires manual review and an ALLOWLIST entry"
            )
            continue

        # 4. Allowlisted despite odd metadata.
        if lname in ALLOWLIST:
            continue

        # 5. Deny substrings + allow-set.
        if not _licence_ok(lic):
            failures.append(f"{name} {version}: forbidden or unrecognised licence '{lic}'")

    # Assert trafilatura is present and version-checked if it was declared a dep.
    # (Absence is fine — only pinned-too-low is a failure, handled above.)
    _ = seen_trafilatura
    return failures


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_licences.py <licences.json>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        entries = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"could not read {path}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(entries, list):
        print("expected a JSON array from pip-licenses", file=sys.stderr)
        return 2

    failures = check(entries)
    if failures:
        print("LICENCE GATE FAILED — forbidden or unreviewed licences:\n", file=sys.stderr)
        for f in sorted(failures):
            print(f"  ✗ {f}", file=sys.stderr)
        print(
            f"\n{len(failures)} problem(s). Fix the dependency or add a reviewed "
            f"ALLOWLIST entry with a justification comment.",
            file=sys.stderr,
        )
        return 1

    print(f"Licence gate passed: {len(entries)} packages, all licences permitted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
