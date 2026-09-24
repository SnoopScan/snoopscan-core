"""Where the CLI keeps its settings between shells.

Environment variables alone were the whole configuration, which meant every
new terminal, cron entry and agent session started unconfigured; the usual
answer is to paste the key into a shell profile, which puts a credential in a
file that gets copied, shared and committed. A small config file the CLI owns
is the thing every comparable tool ships, and it is the difference between
"install it" and "install it and then remember this incantation".

Resolution order, highest first: an explicit flag, then the environment, then
this file. The flag beats the file so one-off overrides work; the environment
beats the file so CI can inject a key without writing to disk.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

# TOML is in the standard library for reading (3.11+) but not for writing, and
# the settings here are flat strings, so the file is written as plain
# `key = "value"` lines and read back with tomllib.
try:  # pragma: no cover - the fallback only runs on 3.10 and below
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

APP = "snoopscan"
KNOWN = ("api_key", "base_url")


def config_path() -> Path:
    """XDG on every platform, because a self-hosted tool ends up on servers."""
    root = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(root) / APP / "config.toml"


def load() -> dict[str, str]:
    path = config_path()
    if tomllib is None or not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        # A corrupt config must not stop the tool running with a flag or an
        # environment variable — those are the ways out of a broken file.
        return {}
    return {k: str(v) for k, v in data.items() if k in KNOWN and isinstance(v, (str, int))}


def save(values: dict[str, str]) -> Path:
    """Merge into the file, creating it 0600.

    The mode is set BEFORE the write, not after: a key written world-readable
    and then chmodded has already been readable, and on a shared box that
    window is the whole vulnerability.
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    merged = {**load(), **{k: v for k, v in values.items() if k in KNOWN}}

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("# snoopscan CLI settings. Written by `snoopscan config set`.\n")
        for key in KNOWN:
            if key in merged:
                fh.write(f'{key} = "{merged[key]}"\n')
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def redact(secret: str) -> str:
    """Enough to tell two keys apart, not enough to use one.

    Printing a key in full is how it reaches a terminal scrollback, a CI log
    and a screenshot; showing nothing at all makes "which key is this?"
    unanswerable, which is the question people run `config show` to ask.
    """
    if not secret:
        return ""
    if len(secret) <= 12:
        return "*" * len(secret)
    return f"{secret[:7]}…{secret[-4:]}"
