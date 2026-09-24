"""A quiet "a newer version is available" line, as every comparable CLI has.

People install a CLI once and run it for months; without a nudge a login flow
or a fixed bug never reaches them. The check asks PyPI at most once a day,
shows the line at most twice a day, goes to stderr so it never mixes with a
command's output, and can never make a command fail. SNOOPSCAN_NO_UPDATE_CHECK=1
turns it off.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

from . import config as user_config

PYPI_URL = "https://pypi.org/pypi/snoopscan/json"
CHECK_EVERY_S = 20 * 3600
SHOW_EVERY_S = 12 * 3600
DISABLE_ENV = "SNOOPSCAN_NO_UPDATE_CHECK"


def _cache_path() -> Path:
    return user_config.config_path().parent / "update-check.json"


def _parse(version: str) -> tuple[int, ...]:
    out = []
    for part in version.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def is_newer(latest: str, current: str) -> bool:
    return _parse(latest) > _parse(current)


def disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").lower() in ("1", "true", "yes")


def _read() -> dict[str, object]:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict[str, object]) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass  # a read-only home must not break a command


def latest_version(now: float | None = None, *, force: bool = False) -> str | None:
    """PyPI's newest version, from a day-old cache when there is one."""
    now = time.time() if now is None else now
    cache = _read()
    checked = cache.get("checked_at")
    if not force and isinstance(checked, (int, float)) and now - checked < CHECK_EVERY_S:
        latest = cache.get("latest")
        return latest if isinstance(latest, str) else None
    try:
        latest = str(httpx.get(PYPI_URL, timeout=2.0).json()["info"]["version"])
    except Exception:  # noqa: BLE001 - offline, blocked, slow: no notice, no error
        return None
    _write({**cache, "latest": latest, "checked_at": now})
    return latest


def notice(current: str, now: float | None = None) -> str | None:
    """The line to print, or None. Shown again only after a while, or for a newer release."""
    if disabled() or current in ("", "0+unknown"):
        return None
    now = time.time() if now is None else now
    latest = latest_version(now)
    if not latest or not is_newer(latest, current):
        return None
    cache = _read()
    shown_at = cache.get("shown_at")
    if (
        cache.get("shown_version") == latest
        and isinstance(shown_at, (int, float))
        and now - shown_at < SHOW_EVERY_S
    ):
        return None
    _write({**cache, "shown_version": latest, "shown_at": now})
    return (
        f"A newer snoopscan is available: {current} -> {latest}\n"
        "  Update: pipx upgrade snoopscan  |  uv tool upgrade snoopscan  |  "
        "python3 -m pip install -U snoopscan\n"
        f"  (npx and uvx always run the latest.)  Silence this: {DISABLE_ENV}=1"
    )
