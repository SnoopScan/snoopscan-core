"""`snoopscan login`: browser sign-in that saves a real key, so nobody copies one.

Before this the CLI's only answer to "no key" was `config set api_key sk_...`
and an agent passed the placeholder straight on to the person.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest

from snoopscan import cli
from snoopscan import config as user_config

REAL_KEY = "sk_live_from_the_browser"


def _site(poll_states: list[str]) -> httpx.MockTransport:
    states = iter(poll_states)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/cli/login/start":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "deviceCode": "d" * 48,
                        "userCode": "ABCD-EFGH",
                        "verifyUrl": "https://site.test/app/cli/ABCD-EFGH",
                        "interval": 1,
                        "expiresIn": 60,
                    },
                },
            )
        assert json.loads(request.content)["deviceCode"] == "d" * 48
        state = next(states)
        data: dict[str, Any] = {"status": state}
        if state == "approved":
            data["apiKey"] = REAL_KEY
        return httpx.Response(200, json={"success": True, "data": data})

    return httpx.MockTransport(handler)


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("SNOOPSCAN_API_KEY", raising=False)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url) or True)
    return opened


def _use(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    real = httpx.Client

    def client(*a: Any, **kw: Any) -> httpx.Client:
        return real(*a, transport=transport, **kw)

    monkeypatch.setattr(cli.httpx, "Client", client)


def test_login_opens_the_browser_waits_for_approval_and_saves_the_key(
    monkeypatch: pytest.MonkeyPatch, isolated: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    _use(monkeypatch, _site(["pending", "pending", "approved"]))
    assert cli.main(["login", "--account-url", "https://site.test"]) == 0

    out = capsys.readouterr().out
    assert "ABCD-EFGH" in out and "https://site.test/app/cli/ABCD-EFGH" in out
    assert REAL_KEY not in out, "the key must never be printed by login"
    assert isolated == ["https://site.test/app/cli/ABCD-EFGH"]
    assert user_config.load()["api_key"] == REAL_KEY
    assert stat.S_IMODE(user_config.config_path().stat().st_mode) == 0o600

    # ...and an agent can read it back to fill in an MCP config itself.
    assert cli.main(["config", "get", "api_key"]) == 0
    assert capsys.readouterr().out.strip() == REAL_KEY


def test_a_cancelled_login_saves_nothing(
    monkeypatch: pytest.MonkeyPatch, isolated: list[str]
) -> None:
    _use(monkeypatch, _site(["pending", "denied"]))
    assert cli.main(["login", "--account-url", "https://site.test", "--no-browser"]) == 1
    assert isolated == [], "--no-browser must not open one"
    assert "api_key" not in user_config.load()


def test_no_key_points_at_login_not_a_placeholder(
    isolated: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["scrape", "https://example.com"]) == 1
    err = capsys.readouterr().err
    assert "snoopscan login" in err
    assert "sk_..." not in err
