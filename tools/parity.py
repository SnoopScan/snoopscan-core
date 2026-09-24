#!/usr/bin/env python3
"""What is actually installed and configured here — printed as JSON, to diff.

Run it on the laptop and on the box and compare. It exists because "it works
locally" has twice meant a capability was missing in production and nothing
said so: camoufox's geoip extra was absent for nine days while the two deepest
fetch tiers silently never ran, and SearXNG — the first rung of the search
ladder — was configured in the ladder but never installed, so one dropped
DuckDuckGo connection took `/v1/search` down entirely.

Both failures share a shape: the code was deployed, the config named the
capability, and the thing itself was not there. A test suite cannot catch that,
because the suite runs where the capability exists.

    python tools/parity.py                    # this machine, as JSON
    python tools/parity.py --diff other.json  # compare against another run

Secrets are never printed — a setting is reported as set/empty, never its value.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import subprocess
import sys
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import Any

# Optional capabilities. Each one has been, or could be, missing in production
# while everything around it looked healthy.
OPTIONAL_IMPORTS = [
    "curl_cffi",  # tier 1, impersonation
    "camoufox",  # tiers 3/3h, stealth
    "browserforge",  # camoufox's fingerprint data
    "playwright",  # tier 2, browser
    "patchright",  # tier 2 hardened
    "selectolax",  # every HTML parse, including the DDG search rung
    "asyncpg",
    "redis",
    "httpx",
    "yaml",
]

SETTING_KEYS = [
    "searxng_url",
    "search_provider",
    "search_ladder",
    "scrapingdog_key",
    "dataforseo_login",
    "dataforseo_password",
    "search_api_key",
    "proxy_enabled",
    "internal_token",
    "encryption_key",
    "user_agent",
    "impersonate_profile",
    "proxy_max_response_mb",
    "proxy_daily_budget_mb",
]

SECRET_HINTS = ("key", "token", "secret", "password", "url", "login")


def _sh(cmd: str) -> str:
    try:
        out = subprocess.run(  # noqa: S603 - fixed commands, defined in this file only
            ["/bin/bash", "-lc", cmd], capture_output=True, text=True, timeout=30
        ).stdout.strip()
        return out or "—"
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}"


def _redact(name: str, value: Any) -> Any:
    """Say whether a thing is configured, never what it is."""
    if value in (None, "", []):
        return "EMPTY"
    if any(h in name.lower() for h in SECRET_HINTS) and isinstance(value, str):
        # A URL is safe to show when it is a loopback address: knowing the
        # search rung points at 127.0.0.1 is the whole point of this check.
        if value.startswith(("http://127.0.0.1", "http://localhost")):
            return value
        # "set", not its length: the two machines hold DIFFERENT secrets by
        # design, so a length is both noise in the diff and a hint about the
        # secret itself.
        return "set"
    return value


def packages() -> dict[str, str]:
    out: dict[str, str] = {}
    for name in OPTIONAL_IMPORTS:
        try:
            import_module(name)
        except Exception as exc:  # noqa: BLE001
            out[name] = f"MISSING ({type(exc).__name__})"
            continue
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            out[name] = "installed"
    return out


def tiers() -> dict[str, Any]:
    """Which fetch tiers this deployment can actually build."""
    try:
        from engine.api.deps import get_fetchers

        built = sorted(str(t) for t in get_fetchers())
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    deep = {"stealth", "stealth_hard", "mobile"}
    return {"built": built, "deepTiersAvailable": bool(deep & set(built))}


def camoufox_browser() -> str:
    """Whether the browser RUNS, not merely whether it was downloaded.

    Reporting the version string was this checker making the same mistake the
    engine made: a version string is a file, and a Camoufox that unpacks
    cleanly still cannot start without the GTK stack, which a server installed
    without a desktop does not have. The live box reported a healthy version
    for its whole life while stealth_hard and mobile were dead (18 Sep 2026).

    `--version` loads the real shared libraries and exits, which is the only
    question worth asking here.
    """
    try:
        from camoufox.pkgman import (  # type: ignore[import-untyped]
            installed_verstr,
            launch_path,
        )

        version = installed_verstr()
        if not version:
            return "NOT DOWNLOADED"
        proc = subprocess.run(  # noqa: S603 - path comes from camoufox, not input
            [str(launch_path()), "--version"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            reason = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
            missing = "missing system libraries" if "shared object" in reason else "will not launch"
            return f"{version} BROKEN ({missing}) - run: playwright install-deps firefox"
        return str(version)
    except Exception as exc:  # noqa: BLE001
        return f"unknown ({type(exc).__name__})"


def settings_view() -> dict[str, Any]:
    try:
        from engine.settings import settings
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:120]}
    return {k: _redact(k, getattr(settings, k, None)) for k in SETTING_KEYS}


def search_view() -> dict[str, Any]:
    try:
        from engine.core import search as serp

        rungs = [p.name for p in serp.ladder()]
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:120]}
    out: dict[str, Any] = {"ladder": rungs}
    try:
        from engine.settings import settings

        if settings.searxng_url:
            import httpx

            r = httpx.get(
                f"{settings.searxng_url.rstrip('/')}/search",
                params={"q": "wikipedia", "format": "json"},
                timeout=15,
            )
            payload = r.json() if r.status_code == 200 else {}
            out["searxngReachable"] = r.status_code == 200
            out["searxngResults"] = len(payload.get("results", []))
        else:
            out["searxngReachable"] = "not configured"
    except Exception as exc:  # noqa: BLE001
        out["searxngReachable"] = f"FAILED ({type(exc).__name__})"
    return out


async def database_view() -> dict[str, Any]:
    try:
        from engine.storage import db
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:120]}
    out: dict[str, Any] = {}
    try:
        out["reachable"] = await db.healthy()
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "error": f"{type(exc).__name__}: {str(exc)[:100]}"}
    try:
        row = await db.fetchrow("SELECT version_num FROM alembic_version")
        out["migration"] = row["version_num"] if row else "none"
        for table in ("api_keys", "owners", "usage_events", "proxy_providers", "domain_profiles"):
            r = await db.fetchrow(f"SELECT count(*) AS n FROM {table}")  # noqa: S608
            out[table] = int(r["n"]) if r else -1
        rows = await db.fetch(
            "SELECT name, priority, enabled FROM proxy_providers ORDER BY priority"
        )
        out["providers"] = [f"{r['name']} p={r['priority']} on={r['enabled']}" for r in rows]
    except Exception as exc:  # noqa: BLE001
        out["query_error"] = f"{type(exc).__name__}: {str(exc)[:100]}"
    finally:
        with contextlib.suppress(Exception):
            await db.close_pool()
    return out


async def snapshot() -> dict[str, Any]:
    return {
        "host": _sh("hostname"),
        "python": sys.version.split()[0],
        "git": _sh("git rev-parse --short HEAD 2>/dev/null"),
        "gitDirty": _sh("git status --porcelain 2>/dev/null | wc -l"),
        "packages": packages(),
        "camoufoxBrowser": camoufox_browser(),
        "tiers": tiers(),
        "settings": settings_view(),
        "search": search_view(),
        "database": await database_view(),
    }


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        out[prefix] = ", ".join(str(x) for x in obj)
    else:
        out[prefix] = obj
    return out


def diff(here: dict[str, Any], there: dict[str, Any]) -> int:
    a, b = flatten(here), flatten(there)
    # Machine-specific facts that SHOULD differ; a mismatch here means nothing.
    # `searxngResults` is a live count off the real web — it varies between two
    # runs on the SAME machine, so a difference says nothing about parity.
    # Whether SearXNG answers at all (`searxngReachable`) very much does.
    ignore = (
        "host",
        "python",
        "gitDirty",
        "database.",
        "search.searxngResults",
    )
    keys = sorted(set(a) | set(b))
    bad = 0
    for k in keys:
        if k.startswith(ignore) or k == "git":
            continue
        if a.get(k) != b.get(k):
            bad += 1
            print(f"  DIFFERS  {k}\n      this: {a.get(k)!r}\n     other: {b.get(k)!r}")
    print(f"\n{bad} difference(s) that matter." if bad else "\nNo differences that matter.")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Environment parity snapshot")
    ap.add_argument("--diff", metavar="FILE", help="compare this machine against a saved snapshot")
    args = ap.parse_args()

    # Building the snapshot imports the engine, which logs to stdout
    # ("browser_tier_enabled" and friends). That lands in the middle of the
    # JSON and makes the output unparseable, so every byte of it goes to
    # stderr and stdout carries the document alone.
    with contextlib.redirect_stdout(sys.stderr):
        snap = asyncio.run(snapshot())
    if args.diff:
        with open(args.diff, encoding="utf-8") as fh:
            return diff(snap, json.load(fh))
    print(json.dumps(snap, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
