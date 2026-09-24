"""The licence gate itself (constraint C2).

The gate is build-breaking, so it needs tests of its own: a gate that silently
stopped denying anything would be worse than no gate, because CI would still
report green.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_GATE_PATH = Path(__file__).resolve().parents[2] / "tools" / "check_licences.py"
_spec = importlib.util.spec_from_file_location("check_licences", _GATE_PATH)
assert _spec and _spec.loader
check_licences = importlib.util.module_from_spec(_spec)
sys.modules["check_licences"] = check_licences
_spec.loader.exec_module(check_licences)


def entry(name: str, licence: str, version: str = "1.0.0") -> dict[str, str]:
    return {"Name": name, "Version": version, "License": licence}


# --------------------------------------------------------------------------
# Permissive licences pass
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "licence",
    [
        "MIT",
        "MIT License",
        "Apache-2.0",
        "Apache Software License",
        "BSD-3-Clause",
        "BSD License",
        "ISC License (ISCL)",
        "MPL-2.0",
        "Mozilla Public License 2.0 (MPL 2.0)",
        "The Unlicense (Unlicense)",
        "PSF-2.0",
    ],
)
def test_permissive_licences_pass(licence: str) -> None:
    assert check_licences.check([entry("somepkg", licence)]) == []


# --------------------------------------------------------------------------
# Copyleft and source-available licences fail
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "licence",
    [
        "AGPL-3.0",
        "GNU Affero General Public License v3",
        "GPL-3.0",
        "GPLv2",
        "LGPL-2.1",
        "GNU Library or Lesser General Public License (LGPL)",
        "SSPL-1.0",
        "CC-BY-NC-4.0",
        "BUSL-1.1",
        "Business Source License",
        "Elastic License 2.0",
        "Proprietary",
    ],
)
def test_forbidden_licences_fail(licence: str) -> None:
    failures = check_licences.check([entry("badpkg", licence)])
    assert failures, f"{licence} must be rejected"


def test_dual_licence_with_a_copyleft_branch_fails() -> None:
    """Copyleft contaminates: 'MIT OR GPL-3.0' is not automatically safe, so
    the gate refuses it rather than silently electing the permissive branch."""
    assert check_licences.check([entry("dual", "MIT OR GPL-3.0")])


# --------------------------------------------------------------------------
# Denied by name, whatever the metadata claims
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["firecrawl", "nodriver", "zendriver", "maxun", "skyvern", "browserless", "rnet"]
)
def test_denied_by_name_even_when_metadata_says_mit(name: str) -> None:
    """Repo metadata lies. These are denied on name regardless."""
    failures = check_licences.check([entry(name, "MIT")])
    assert failures
    assert "denied by name" in failures[0]


# --------------------------------------------------------------------------
# Version floors
# --------------------------------------------------------------------------


def test_trafilatura_below_1_8_fails() -> None:
    """Earlier releases are GPLv3+; only 1.8.0 onward is Apache-2.0."""
    assert check_licences.check([entry("trafilatura", "Apache-2.0", "1.7.0")])


def test_trafilatura_at_or_above_1_8_passes() -> None:
    assert check_licences.check([entry("trafilatura", "Apache-2.0", "1.8.0")]) == []
    assert check_licences.check([entry("trafilatura", "Apache-2.0", "2.0.1")]) == []


# --------------------------------------------------------------------------
# Unknown metadata fails closed
# --------------------------------------------------------------------------


def test_unknown_licence_fails_and_demands_review() -> None:
    failures = check_licences.check([entry("mystery", "UNKNOWN")])
    assert failures
    assert "manual review" in failures[0]


def test_missing_licence_fails() -> None:
    assert check_licences.check([{"Name": "mystery", "Version": "1.0", "License": ""}])


def test_allowlisted_package_passes_with_a_recorded_reason() -> None:
    """Every allowlist entry carries a justification; the entry itself is the
    audit record."""
    for name, reason in check_licences.ALLOWLIST.items():
        assert reason.strip(), f"allowlist entry {name} has no justification"
        assert check_licences.check([entry(name, "UNKNOWN")]) == []
