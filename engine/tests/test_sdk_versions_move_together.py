"""The Python and JS SDKs are released together, so they carry one number.

They drifted to 0.2.4 and 0.1.4 while doing exactly the same things — Python
was two weeks older and had taken a minor bump before the JS SDK existed —
and the only question the mismatch ever raised was "why are these
different?". From 0.3.0 they move together, and the JS lockfile moves with
its package.json: it was left at 0.1.3 through the whole 0.1.4 release.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def python_version() -> str:
    text = (ROOT / "sdk/python/pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.M)
    assert match, "no version in sdk/python/pyproject.toml"
    return match.group(1)


def test_both_sdks_carry_the_same_version() -> None:
    js = json.loads((ROOT / "sdk/js/package.json").read_text(encoding="utf-8"))["version"]
    assert python_version() == js, f"Python {python_version()} and JS {js} have drifted apart"


def test_the_js_lockfile_agrees_with_its_package() -> None:
    package = json.loads((ROOT / "sdk/js/package.json").read_text(encoding="utf-8"))["version"]
    lock = json.loads((ROOT / "sdk/js/package-lock.json").read_text(encoding="utf-8"))
    assert lock["version"] == package
    assert lock["packages"][""]["version"] == package
