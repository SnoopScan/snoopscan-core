"""Keeping people current, and one command that checks a whole install.

A CLI installed once and run for months never learns about a login flow or a
fix unless it says so; and an agent that cannot tell a bad key from a missing
one, or an old Python from an unreachable API, guesses.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from snoopscan import _update, cli
from snoopscan import config as user_config


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("SNOOPSCAN_API_KEY", raising=False)
    return tmp_path


def _pypi_says(monkeypatch: pytest.MonkeyPatch, version: str | None) -> list[str]:
    asked: list[str] = []

    def fake_get(url: str, **kw: Any) -> httpx.Response:
        asked.append(url)
        if version is None:
            raise httpx.ConnectError("offline")
        return httpx.Response(200, json={"info": {"version": version}})

    monkeypatch.setattr(_update.httpx, "get", fake_get)
    return asked


def test_versions_compare_as_numbers_not_text() -> None:
    assert _update.is_newer("0.10.0", "0.9.9")
    assert not _update.is_newer("0.4.1", "0.4.1")
    assert not _update.is_newer("0.4.0", "0.4.1")


def test_a_newer_release_is_announced_then_left_alone_for_a_while(
    monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    monkeypatch.delenv("SNOOPSCAN_NO_UPDATE_CHECK")
    asked = _pypi_says(monkeypatch, "9.0.0")
    first = _update.notice("0.5.0", now=1_000_000)
    assert first and "0.5.0 -> 9.0.0" in first and "pipx upgrade snoopscan" in first
    # Asked PyPI once; the next runs use the day-old cache and stay quiet.
    assert _update.notice("0.5.0", now=1_000_060) is None
    assert len(asked) == 1
    # Twelve hours on, it is shown again.
    assert _update.notice("0.5.0", now=1_000_000 + 13 * 3600)


def test_the_notice_never_shows_when_current_offline_or_switched_off(
    monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    monkeypatch.delenv("SNOOPSCAN_NO_UPDATE_CHECK")
    _pypi_says(monkeypatch, "0.5.0")
    assert _update.notice("0.5.0", now=1) is None
    _pypi_says(monkeypatch, None)
    assert _update.notice("0.5.0", now=1_000_000) is None  # offline: silent, no error
    monkeypatch.setenv("SNOOPSCAN_NO_UPDATE_CHECK", "1")
    _pypi_says(monkeypatch, "9.0.0")
    assert _update.notice("0.5.0", now=5_000_000) is None


def _api(monkeypatch: pytest.MonkeyPatch, key_status: int) -> None:
    def fake_get(url: str, **kw: Any) -> httpx.Response:
        if url.endswith("/v1/templates"):
            return httpx.Response(key_status, json={})
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(cli.httpx, "get", fake_get)
    monkeypatch.setattr(_update, "latest_version", lambda **kw: None)


def test_doctor_passes_a_working_install(
    monkeypatch: pytest.MonkeyPatch, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    user_config.save({"api_key": "sk_live_abcdefghijkl"})
    _api(monkeypatch, 200)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "accepted" in out and "All good." in out
    assert "sk_live_abcdefghijkl" not in out, "doctor must not print the key"


def test_doctor_says_how_to_fix_a_missing_or_refused_key(
    monkeypatch: pytest.MonkeyPatch, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _api(monkeypatch, 200)
    assert cli.main(["doctor"]) == 1
    assert "not set. Fix: snoopscan login" in capsys.readouterr().out

    user_config.save({"api_key": "sk_live_revoked_key_xx"})
    _api(monkeypatch, 401)
    assert cli.main(["doctor"]) == 1
    assert "not recognised" in capsys.readouterr().out


def test_python_dash_m_runs_the_cli_when_the_command_is_not_on_the_path() -> None:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    r = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "snoopscan", "--help"], capture_output=True, text=True, env=env
    )
    assert r.returncode == 0
    assert "login" in r.stdout and "doctor" in r.stdout
