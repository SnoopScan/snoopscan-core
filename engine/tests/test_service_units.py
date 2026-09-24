"""Both supervisors run the same three services.

launchd owns them on a laptop, systemd on a Linux server. Kept by hand, the two
disagree about which module a worker runs, and the disagreement only shows up on
whichever machine nobody tested — the same failure as every other duplicated
list here. `engine/services.py` is the one definition; this pins both to it.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from engine.services import SERVICES, by_name

ROOT = Path(__file__).resolve().parents[2]
LAUNCHD = ROOT / "deploy" / "launchd"
SYSTEMD = ROOT / "deploy" / "systemd"


@pytest.mark.parametrize("service", SERVICES, ids=lambda s: s.name)
def test_a_systemd_unit_exists_for_every_service(service) -> None:
    assert (SYSTEMD / f"snoopscan-{service.name}.service").is_file()


@pytest.mark.parametrize("service", SERVICES, ids=lambda s: s.name)
def test_a_launchd_plist_exists_for_every_service(service) -> None:
    assert (LAUNCHD / f"com.snoopscan.{service.name}.plist.template").is_file()


@pytest.mark.parametrize("service", SERVICES, ids=lambda s: s.name)
def test_the_systemd_unit_runs_what_the_definition_says(service) -> None:
    unit = (SYSTEMD / f"snoopscan-{service.name}.service").read_text()
    exec_line = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    assert exec_line.endswith(" " + " ".join(service.args)), exec_line


@pytest.mark.parametrize("service", SERVICES, ids=lambda s: s.name)
def test_the_launchd_plist_runs_what_the_definition_says(service) -> None:
    """The plists are hand-written for their comments; this is what stops them
    drifting from the manifest the systemd units are generated from."""
    raw = (LAUNCHD / f"com.snoopscan.{service.name}.plist.template").read_bytes()
    args = plistlib.loads(raw)["ProgramArguments"]
    assert tuple(args[1:]) == service.args, f"{service.name}: {args[1:]} != {service.args}"


def test_the_generated_units_are_committed_current() -> None:
    """CI runs `render_units.py --check`; this fails in the suite too, so a
    developer sees it before the push rather than after."""
    import subprocess
    import sys

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "tools" / "render_units.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_service_runs_python_dash_m() -> None:
    """A console script is a `#!/bin/sh` wrapper when the venv path contains a
    space, and neither supervisor can exec one out of a protected directory."""
    for service in SERVICES:
        assert service.args[0] == "-m", f"{service.name} does not use python -m"


def test_the_lookup_refuses_an_unknown_name() -> None:
    with pytest.raises(KeyError):
        by_name("nope")
