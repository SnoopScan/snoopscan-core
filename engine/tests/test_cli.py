"""The CLI, tested rather than tried by hand.

Written after being pulled up on exactly this: the CLI shipped with eleven
commands and no coverage, verified only by running it a few times. Every bug
found in it that day — a missing `Document.raw`, a 4.2 MB dump to stdout, a
doubled error prefix, and a `--quiet` that argparse silently discarded — would
have been caught by one of the assertions below.

The client is faked. These test the CLI's own behaviour: argument shapes,
output routing, exit codes. The client's behaviour is covered by test_sdk.
"""

from __future__ import annotations

import json
import os
import stat
from typing import Any

import pytest
from snoopscan import config as user_config
from snoopscan.cli import EXIT_BLOCKED, EXIT_ERROR, EXIT_OK, build_parser, main
from snoopscan.client import Cost, Document, SnoopScanError


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path_factory, monkeypatch):
    """Never read the developer's own config.

    The CLI resolves its key from ~/.config/snoopscan/config.toml, so without
    this every assertion about an UNCONFIGURED install silently depends on
    whether whoever runs the suite happens to have configured it. The exported
    suite caught this the first time it ran on a machine with a key stored.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("config")))
    monkeypatch.delenv("SNOOPSCAN_API_KEY", raising=False)
    monkeypatch.delenv("SNOOPSCAN_BASE_URL", raising=False)


class FakeClient:
    """Records what the CLI asked for, and answers plausibly."""

    def __init__(self, **behaviour: Any) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.behaviour = behaviour
        self.base_url = "http://engine.test"

    def _record(self, name: str, *a: Any, **k: Any) -> Any:
        self.calls.append((name, a, k))
        if name in self.behaviour:
            value = self.behaviour[name]
            if isinstance(value, Exception):
                raise value
            return value
        raise AssertionError(f"unexpected call: {name}")

    def __getattr__(self, name: str) -> Any:
        return lambda *a, **k: self._record(name, *a, **k)

    def close(self) -> None:
        pass


def _doc(markdown: str = "# Hello", **raw: Any) -> Document:
    payload = {"markdown": markdown, "metadata": {}, "cost": {"tier": "http"}, **raw}
    return Document(
        markdown=markdown,
        metadata=payload.get("metadata", {}),
        cost=Cost(tier="http"),
        raw=payload,
    )


@pytest.fixture
def run(monkeypatch: Any):
    """Run the CLI with a faked client, returning (exit_code, client)."""

    def _run(argv: list[str], **behaviour: Any) -> tuple[int, FakeClient]:
        client = FakeClient(**behaviour)
        monkeypatch.setattr("snoopscan.cli.SnoopScan", lambda *a, **k: client)
        monkeypatch.setenv("SNOOPSCAN_API_KEY", "sk_test")
        return main(argv), client

    return _run


# -- the shape of the interface ------------------------------------------


def test_every_command_is_reachable() -> None:
    """A verb in the help text that cannot be run is a broken promise."""
    parser = build_parser()
    commands = set(parser._subparsers._group_actions[0].choices)
    assert commands == {
        "scrape",
        "crawl",
        "crawl-status",
        "map",
        "search",
        "extract",
        "parse",
        "products",
        "posts",
        "company",
        "domain",
        "monitor",
        "status",
        "config",
        "login",
        "doctor",
    }


def test_no_command_is_named_for_something_unbuilt() -> None:
    """`snoop` is reserved for the agent and must NOT appear until it exists;
    `scan` was dropped for being undefined."""
    commands = set(build_parser()._subparsers._group_actions[0].choices)
    assert "snoop" not in commands
    assert "scan" not in commands


@pytest.mark.parametrize("flag", ["--json", "--quiet", "--pretty"])
def test_global_flags_work_after_the_subcommand(flag: str, run: Any) -> None:
    """argparse silently DISCARDED these when they were on both parsers —
    the flag parsed and then the subparser's default overwrote it."""
    code, client = run(["scrape", "https://x.test", flag], scrape=_doc())
    assert code == EXIT_OK
    assert client.calls[0][0] == "scrape"


# -- output routing ------------------------------------------------------


def test_content_goes_to_stdout_and_receipts_do_not(capsys: Any, run: Any) -> None:
    """`snoopscan scrape url > page.md` must produce a clean file."""
    run(["scrape", "https://x.test"], scrape=_doc("# Title\n\nBody."))
    out = capsys.readouterr()
    assert out.out.strip() == "# Title\n\nBody."
    assert "tier=http" in out.err


def test_output_file_receives_the_content(tmp_path: Any, run: Any) -> None:
    target = tmp_path / "page.md"
    run(["scrape", "https://x.test", "-o", str(target)], scrape=_doc("# Written"))
    assert target.read_text() == "# Written"


def test_json_returns_the_payload_not_just_markdown(capsys: Any, run: Any) -> None:
    run(["scrape", "https://x.test", "--json"], scrape=_doc("# T"))
    assert json.loads(capsys.readouterr().out)["markdown"] == "# T"


def test_a_catalogue_is_summarised_not_dumped(capsys: Any, run: Any) -> None:
    """One blog returned 4.2 MB of post bodies to a terminal. Full fidelity is
    one flag away; the default must be readable."""
    posts = {
        "platform": "wordpress",
        "source": "api",
        "posts": [
            {"title": "One", "url": "https://x.test/1", "content_html": "x" * 50_000},
            {"title": "Two", "url": "https://x.test/2", "content_html": "y" * 50_000},
        ],
    }
    run(["posts", "https://x.test"], posts=posts)
    out = capsys.readouterr().out
    assert "One" in out and "https://x.test/1" in out
    assert len(out) < 1_000, "the bodies must not be dumped by default"


# -- failure, and telling the kinds apart ---------------------------------


def test_a_refusal_exits_differently_from_a_broken_request(run: Any) -> None:
    """The whole point: retrying a TARGET_ERROR forever is the classic waste."""
    blocked, _ = run(["scrape", "https://x.test"], scrape=SnoopScanError("BLOCKED", "refused"))
    target, _ = run(["scrape", "https://x.test"], scrape=SnoopScanError("TARGET_ERROR", "404"))
    assert blocked == EXIT_BLOCKED
    assert target == EXIT_ERROR


def test_robots_denied_is_also_a_refusal(run: Any) -> None:
    code, _ = run(["scrape", "https://x.test"], scrape=SnoopScanError("ROBOTS_DENIED", "no"))
    assert code == EXIT_BLOCKED


def test_the_error_code_is_not_printed_twice(capsys: Any, run: Any) -> None:
    """`str(exc)` already carries the code — printing it again read
    'BLOCKED: BLOCKED: ...'."""
    run(["scrape", "https://x.test"], scrape=SnoopScanError("BLOCKED", "refused"))
    assert capsys.readouterr().err.count("BLOCKED") == 1


def test_a_missing_key_fails_before_any_request(capsys: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv("SNOOPSCAN_API_KEY", raising=False)
    assert main(["scrape", "https://x.test"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "snoopscan config set api_key" in err, "the fix must be in the message"
    assert "SNOOPSCAN_API_KEY" in err


def test_status_needs_no_key(monkeypatch: Any) -> None:
    """Diagnosing an unconfigured install must not require configuration."""
    monkeypatch.delenv("SNOOPSCAN_API_KEY", raising=False)
    parser = build_parser()
    args = parser.parse_args(["status"])
    assert args.command == "status"


# -- options reach the client ---------------------------------------------


def test_scrape_options_are_passed_through(run: Any) -> None:
    _, client = run(
        [
            "scrape",
            "https://x.test",
            "--tier",
            "browser",
            "--max-age",
            "0",
            "--formats",
            "markdown,html",
        ],
        scrape=_doc(),
    )
    _, _, kwargs = client.calls[0]
    assert kwargs["tier"] == "browser"
    assert kwargs["maxAge"] == 0
    assert kwargs["formats"] == ["markdown", "html"]


def test_unset_options_are_not_sent(run: Any) -> None:
    """Sending our defaults would override the API's own."""
    _, client = run(["scrape", "https://x.test"], scrape=_doc())
    assert client.calls[0][2] == {}


# --------------------------------------------------------------------------
# Stored configuration
#
# The CLI read the key from the environment and nowhere else, so every new
# shell, cron entry and agent session started unconfigured — one of them hit
# exactly that and stopped, unable to run at all. These cover the file, the
# order the sources are consulted in, and the fact that a key must not be
# printed back in full.
# --------------------------------------------------------------------------


@pytest.fixture
def config_home(monkeypatch, tmp_path):
    """A writable config dir of this test's own. `_isolate_user_config`
    already redirects XDG_CONFIG_HOME; this narrows it to one test."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def test_config_round_trips(config_home):
    user_config.save({"api_key": "sk_stored", "base_url": "http://engine.test"})

    assert user_config.load() == {"api_key": "sk_stored", "base_url": "http://engine.test"}


def test_config_file_is_not_world_readable(config_home):
    path = user_config.save({"api_key": "sk_secret"})

    # A credential written 0644 has already leaked on a shared machine.
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_config_set_merges_rather_than_replaces(config_home):
    user_config.save({"api_key": "sk_one"})
    user_config.save({"base_url": "http://second"})

    stored = user_config.load()
    assert stored["api_key"] == "sk_one", "writing one setting erased the other"
    assert stored["base_url"] == "http://second"


def test_config_ignores_unknown_keys(config_home):
    user_config.save({"api_key": "sk_one", "sneaky": "value"})

    assert "sneaky" not in user_config.load()


def test_a_corrupt_config_does_not_stop_the_tool(config_home):
    path = user_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not toml {{{")

    # It must still be possible to run with a flag or an env var.
    assert user_config.load() == {}


def test_redact_shows_enough_to_identify_and_not_enough_to_use():
    shown = user_config.redact("sk_FAKEkeyDoNotUseThisAnywhere0")

    assert "sk_FAKE" in shown
    assert "ere0" not in shown or len(shown) < 20
    assert user_config.redact("short") == "*****"


def test_stored_key_is_used_when_the_environment_has_none(config_home, monkeypatch, capsys):
    user_config.save({"api_key": "sk_stored"})
    seen: dict[str, str] = {}

    class Recorder(FakeClient):
        def __init__(self, key, base_url, **kw):
            super().__init__(scrape=Document(markdown="hi", metadata={}, cost=Cost(tier="http")))
            seen["key"] = key
            seen["base_url"] = base_url

    monkeypatch.setattr("snoopscan.cli.SnoopScan", Recorder)
    assert main(["scrape", "https://example.com"]) == EXIT_OK
    assert seen["key"] == "sk_stored"


def test_the_environment_beats_the_stored_file(config_home, monkeypatch):
    user_config.save({"api_key": "sk_stored"})
    monkeypatch.setenv("SNOOPSCAN_API_KEY", "sk_from_env")
    seen: dict[str, str] = {}

    class Recorder(FakeClient):
        def __init__(self, key, base_url, **kw):
            super().__init__(scrape=Document(markdown="hi", metadata={}, cost=Cost(tier="http")))
            seen["key"] = key

    monkeypatch.setattr("snoopscan.cli.SnoopScan", Recorder)
    main(["scrape", "https://example.com"])
    assert seen["key"] == "sk_from_env", (
        "CI must be able to inject a key without writing one to disk"
    )


def test_the_flag_beats_everything(config_home, monkeypatch):
    user_config.save({"api_key": "sk_stored"})
    monkeypatch.setenv("SNOOPSCAN_API_KEY", "sk_from_env")
    seen: dict[str, str] = {}

    class Recorder(FakeClient):
        def __init__(self, key, base_url, **kw):
            super().__init__(scrape=Document(markdown="hi", metadata={}, cost=Cost(tier="http")))
            seen["key"] = key

    monkeypatch.setattr("snoopscan.cli.SnoopScan", Recorder)
    main(["scrape", "https://example.com", "--api-key", "sk_flag"])
    assert seen["key"] == "sk_flag"


def test_config_show_never_prints_the_whole_key(config_home, capsys):
    user_config.save({"api_key": "sk_FAKEkeyDoNotUseThisAnywhere0"})

    assert main(["config", "show"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "sk_FAKEkeyDoNotUseThisAnywhere0" not in out, "config show leaked the key"
    assert "sk_FAKE" in out


def test_config_show_says_when_the_environment_is_overriding(config_home, monkeypatch, capsys):
    user_config.save({"api_key": "sk_stored"})
    monkeypatch.setenv("SNOOPSCAN_API_KEY", "sk_from_env")

    main(["config", "show"])
    assert "overrides" in capsys.readouterr().out


def test_config_and_status_run_without_a_key(config_home):
    # These two are how you DIAGNOSE having no key; demanding one to run them
    # is a locked door with the key inside.
    assert main(["config", "path"]) == EXIT_OK


def test_the_missing_key_message_names_the_fix(config_home, capsys):
    assert main(["scrape", "https://example.com"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "snoopscan config set api_key" in err
