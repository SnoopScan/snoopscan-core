"""The launchd agents that own the long-running services (6 Sep 2026).

Run from a terminal tab these services die with the tab, and a worker left from
an older session keeps eating jobs off the shared queue on stale code — which
orphaned a 500-page crawl. These tests pin the properties that make the agents
do their job, because a broken plist fails silently: launchd simply declines to
load it and nothing runs.
"""

from __future__ import annotations

import plistlib
import stat
from pathlib import Path

import pytest

AGENTS = Path(__file__).resolve().parents[2] / "deploy" / "launchd"

_PREFIX, _SUFFIX = "com.snoopscan.", ".plist.template"


def _services() -> tuple[str, ...]:
    """Discovered from the templates, so a new service is covered the moment it
    exists. A hard-coded triple silently exempts the fourth one from every test
    in this file — the case where the coverage is most wanted."""
    return tuple(
        sorted(p.name[len(_PREFIX) : -len(_SUFFIX)] for p in AGENTS.glob(f"{_PREFIX}*{_SUFFIX}"))
    )


SERVICES = _services()


def test_the_services_are_discovered_not_assumed() -> None:
    """A guard on the discovery itself: an empty glob would make every
    parametrised test below vacuously pass by having no cases at all."""
    assert set(SERVICES) >= {"api", "worker", "scheduler"}, SERVICES


def _plist(service: str) -> dict:
    raw = (AGENTS / f"com.snoopscan.{service}.plist.template").read_text()
    # The placeholders are substituted at install time; a path with a space is
    # the realistic case here.
    resolved = raw.replace("__REPO__", "/Users/x/Long Path/eng").replace(
        "__LOGS__", "/Users/x/Library/Logs/snoopscan"
    )
    return plistlib.loads(resolved.encode())


@pytest.mark.parametrize("service", SERVICES)
def test_each_template_is_a_valid_plist_with_the_right_label(service: str) -> None:
    assert _plist(service)["Label"] == f"com.snoopscan.{service}"


@pytest.mark.parametrize("service", SERVICES)
def test_each_agent_starts_at_login_and_restarts_on_crash(service: str) -> None:
    d = _plist(service)
    assert d["RunAtLoad"] is True, "a service that needs starting by hand is the old problem"
    assert d["KeepAlive"] is True, "the point is that it comes back"
    assert d["ThrottleInterval"] >= 10, "a bad config must not thrash"


@pytest.mark.parametrize("service", SERVICES)
def test_the_working_directory_is_the_repo_so_dotenv_loads(service: str) -> None:
    """settings.py reads `.env` relative to the working directory."""
    assert _plist(service)["WorkingDirectory"] == "/Users/x/Long Path/eng"


@pytest.mark.parametrize("service", SERVICES)
def test_arguments_are_an_array_so_a_space_in_the_path_survives(service: str) -> None:
    args = _plist(service)["ProgramArguments"]
    assert isinstance(args, list) and len(args) >= 2
    assert args[0].startswith("/Users/x/Long Path/eng/.venv/bin/")
    assert " " in args[0], "this is the case a single shell string would split"


def test_each_service_runs_the_thing_it_says_it_does() -> None:
    assert "engine.api.app:app" in _plist("api")["ProgramArguments"]
    assert "engine.workers.http_worker" in _plist("worker")["ProgramArguments"]
    assert "engine.workers.scheduler" in _plist("scheduler")["ProgramArguments"]


@pytest.mark.parametrize("service", SERVICES)
def test_no_agent_execs_a_console_script(service: str) -> None:
    """argv[0] must be the interpreter itself, never a `.venv/bin/<name>` entry
    point.

    pip writes those as `#!/bin/sh` wrappers whenever the venv path contains a
    space — and this repo's does. launchd then execs /bin/sh, which is denied
    reading the script out of ~/Documents under macOS TCC: the API agent died
    with exit 126 and "Operation not permitted" while the worker and scheduler,
    on the same repo and the same venv, ran fine (6 Sep 2026). `python -m` has
    no such wrapper.
    """
    args = _plist(service)["ProgramArguments"]
    assert args[0].endswith("/python"), f"{service} execs {args[0]!r}, not the interpreter"
    assert args[1] == "-m", f"{service} must run its entry point with `python -m`"


def test_the_api_agent_does_not_run_the_reload_supervisor() -> None:
    """--reload runs a second supervising process; a service manager owns one."""
    assert "--reload" not in _plist("api")["ProgramArguments"]


@pytest.mark.parametrize("service", SERVICES)
def test_each_agent_writes_its_own_log(service: str) -> None:
    d = _plist(service)
    assert d["StandardOutPath"].endswith(f"/{service}.log")
    assert d["StandardErrorPath"] == d["StandardOutPath"]


def test_the_installer_is_executable_and_offers_a_way_out() -> None:
    script = AGENTS / "install.sh"
    assert script.stat().st_mode & stat.S_IXUSR, "an installer nobody can run is not one"
    body = script.read_text()
    assert "--stop" in body, "installing without uninstalling is a trap"
    assert "launchctl bootstrap" in body and "launchctl bootout" in body
    # The guard that stops two of anything fighting over the port and the queue.
    assert "pgrep -f" in body and "engine.workers." in body
