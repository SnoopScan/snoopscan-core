#!/usr/bin/env python3
"""Read the top of the web, and say honestly whose fault every failure is.

A pass rate on its own is not actionable: "23 of 250 failed" cannot tell a
hostile site from a bug of ours. So every failure is fetched a second time by
a plain HTTP client with a browser User-Agent, direct, no proxy, no engine. If
that gets the page and the engine did not, the failure is OURS and the row is
marked `self_inflicted`.

That column is the point of this tool. A retail home page carrying a Turnstile
widget read as "blocked by Cloudflare" for a week; a plain curl of the same URL
returned it in full. One run of this would have found it in seconds.

Results are kept (benchmark_runs / benchmark_results) so successive runs show
drift: a rung that quietly stopped working, a signature that started firing on
real pages, a vendor whose IPs went bad.

    python3 tools/benchmark.py --top 250            # Tranco top sites
    python3 tools/benchmark.py --urls a.com b.com   # a named set
    python3 tools/benchmark.py --report <run_id>    # read a past run back

Run it on the box: it needs the engine's database, and it calls the live API
the way a customer does, so what it measures is the product, not a fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ENV = Path("/srv/snoopscan/.env")
API = "https://api.snoopscan.com/v1/scrape"
TRANCO = Path("/tmp/top-1m.csv")  # noqa: S108 - operator tool, a list you downloaded
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# Domains that serve no page to a reader: ad exchanges, CDNs, telemetry and
# API hosts. They are in every popularity list and would read as failures
# while telling us nothing about whether the engine can read the web.
INFRA = re.compile(
    r"(^|\.)(akamai|akamaized|akadns|edgekey|edgesuite|gstatic|googleapis|googlesyndication|"
    r"googletagmanager|googleadservices|doubleclick|adsrvr|adnxs|rubiconproject|criteo|"
    r"casalemedia|pubmatic|openx|taboola|outbrain|amazon-adsystem|cloudfront|fastly|"
    r"fbcdn|twimg|licdn|cdninstagram|ytimg|gvt1|gvt2|azureedge|windowsupdate|office365|"
    r"skype|meraki|rlcdn|demdex|omtrdc|scorecardresearch|crashlytics|sentry|segment|"
    r"branch|appsflyer|adjust|bluekai|everesttech|1rx|3lift|smartadserver|teads|zemanta|"
    r"yieldmo|sharethrough|districtm|indexww|bidswitch|mathtag|agkn|tapad|crwdcntrl|"
    r"ntp|ntpool|root-servers|in-addr|arpa|local)\.",
    re.IGNORECASE,
)


def _head_sha() -> str:
    """Which build produced these numbers. Sync: it runs once, before the work."""
    out = subprocess.run(  # noqa: S603 - reading our own HEAD
        ["/usr/bin/git", "rev-parse", "--short", "HEAD"],
        cwd="/srv/snoopscan",
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip()


def from_env(key: str) -> str:
    try:
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def targets(top: int) -> list[tuple[int, str]]:
    """The most popular sites that actually serve a page to a reader."""
    if not TRANCO.exists():
        sys.exit(f"{TRANCO} not found. Download the list first (tranco-list.eu).")
    out: list[tuple[int, str]] = []
    for line in TRANCO.read_text(encoding="utf-8").splitlines():
        rank, _, domain = line.partition(",")
        domain = domain.strip().lower()
        if not domain or INFRA.search(domain + "."):
            continue
        out.append((int(rank), domain))
        if len(out) >= top:
            break
    return out


def _words(html: str) -> int:
    text = re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S))
    return len(text.split())


async def _run(cmd: list[str], seconds: int) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(  # noqa: S603 - fixed argv
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=seconds)
    except TimeoutError:
        proc.kill()
        return 1, ""
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def control_fetch(url: str) -> tuple[bool, int]:
    """What a plain client gets, direct: the second opinion on every failure."""
    code, body = await _run(
        [
            "/usr/bin/curl",
            "-sL",
            "--max-time",
            "25",
            "--compressed",
            "-A",
            BROWSER_UA,
            "-H",
            "Accept-Language: en-GB,en;q=0.9",
            url,
        ],  # fmt: skip
        seconds=40,
    )
    if code != 0 or not body:
        return False, 0
    words = _words(body)
    return words > 100, words


async def scrape(key: str, url: str, timeout_ms: int) -> dict[str, Any]:
    body = {"url": url, "formats": ["markdown"], "maxAge": 0, "timeout": timeout_ms}
    started = time.monotonic()
    code, out = await _run(
        [
            "/usr/bin/curl",
            "-s",
            "--max-time",
            str(timeout_ms // 1000 + 20),
            "-X",
            "POST",
            API,
            "-H",
            f"Authorization: Bearer {key}",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(body),
        ],  # fmt: skip
        seconds=timeout_ms // 1000 + 40,
    )
    elapsed = int((time.monotonic() - started) * 1000)
    if code != 0 or not out.strip():
        return {"ok": False, "error_code": "NO_RESPONSE", "elapsed_ms": elapsed}
    try:
        payload = json.loads(out)
    except ValueError:
        return {"ok": False, "error_code": "BAD_JSON", "elapsed_ms": elapsed}
    if payload.get("success"):
        data = payload["data"]
        cost = data.get("cost") or {}
        return {
            "ok": True,
            "tier": cost.get("tier"),
            "tiers_attempted": cost.get("tiers_attempted"),
            "words": (data.get("metadata") or {}).get("wordCount"),
            "credits": cost.get("credits"),
            "proxy_bytes": cost.get("proxy_bytes"),
            "elapsed_ms": elapsed,
        }
    err = payload.get("error") or {}
    detail = err.get("detail") or {}
    return {
        "ok": False,
        "error_code": err.get("code"),
        "signal": detail.get("final_signal") or detail.get("signal"),
        "tiers_attempted": detail.get("tiers_attempted"),
        "elapsed_ms": elapsed,
    }


class Pace:
    """Keep the run under the key's own rate limit.

    Without this the benchmark trips OUR limit and reports it as the web
    failing: 69 of 139 failures in the first run were RATE_LIMITED, which says
    nothing about any site (20 Sep 2026).
    """

    def __init__(self, per_minute: int) -> None:
        self._gap = 60.0 / max(per_minute, 1)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = asyncio.get_running_loop().time()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._gap
        if delay:
            await asyncio.sleep(delay)


async def one(
    key: str, rank: int, domain: str, timeout_ms: int, pace: Pace | None = None
) -> dict[str, Any]:
    url = f"https://{domain}/"
    if pace is not None:
        await pace.wait()
    row = await scrape(key, url, timeout_ms)
    if row.get("error_code") == "RATE_LIMITED":
        # Ours, not the site's. Wait it out once rather than record a failure.
        await asyncio.sleep(20)
        if pace is not None:
            await pace.wait()
        row = await scrape(key, url, timeout_ms)
    row |= {"rank": rank, "domain": domain, "url": url}
    if not row["ok"]:
        control_ok, control_words = await control_fetch(url)
        row |= {
            "control_ok": control_ok,
            "control_words": control_words,
            # Ours only when a plain client got a real page and we did not.
            "self_inflicted": control_ok,
        }
    return row


async def main() -> int:
    ap = argparse.ArgumentParser(description="Benchmark the engine against the live web")
    ap.add_argument("--top", type=int, default=250, help="how many popular sites")
    ap.add_argument("--urls", nargs="*", help="specific domains instead of the list")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--timeout-ms", type=int, default=90_000)
    ap.add_argument("--rpm", type=int, default=240, help="stay under the key's limit")
    ap.add_argument("--label", default="")
    ap.add_argument("--report", help="print a past run instead of running one")
    ap.add_argument("--no-store", action="store_true", help="do not write to the database")
    args = ap.parse_args()

    sys.path.insert(0, "/srv/snoopscan")
    from engine.storage import repositories as repo

    if args.report:
        failures = await repo.benchmark_failures(args.report)
        for f in failures:
            blame = "OURS " if f["self_inflicted"] else "site "
            print(
                f"{blame} {str(f['rank'] or '-'):>4} {f['domain']:<28} "
                f"{str(f['error_code']):<18} {str(f['signal'] or ''):<22} "
                f"control={f['control_words']}w"
            )
        print(f"\n{len(failures)} failures, {sum(1 for f in failures if f['self_inflicted'])} ours")
        return 0

    key = from_env("BENCHMARK_API_KEY") or from_env("BAKEOFF_API_KEY")
    if not key:
        sys.exit("No API key in the env file (BENCHMARK_API_KEY or BAKEOFF_API_KEY).")

    sites = (
        [(0, d.replace("https://", "").replace("http://", "").strip("/")) for d in args.urls]
        if args.urls
        else targets(args.top)
    )
    sha = _head_sha()
    label = args.label or f"top {len(sites)}"
    run_id = "" if args.no_store else await repo.start_benchmark_run(label, sha or None)
    where = run_id or "(not stored)"
    print(f"benchmark: {len(sites)} sites, {args.concurrency} at a time, run {where}\n", flush=True)

    gate = asyncio.Semaphore(args.concurrency)
    pace = Pace(args.rpm)
    done = 0
    rows: list[dict[str, Any]] = []

    async def work(rank: int, domain: str) -> None:
        nonlocal done
        async with gate:
            row = await one(key, rank, domain, args.timeout_ms, pace)
        rows.append(row)
        if run_id:
            await repo.record_benchmark_result(run_id, row)
        done += 1
        mark = "ok  " if row["ok"] else ("OURS" if row.get("self_inflicted") else "site")
        detail = (
            f"{row.get('tier')} {row.get('words')}w"
            if row["ok"]
            else f"{row.get('error_code')} {row.get('signal') or ''} "
            f"control={row.get('control_words', 0)}w"
        )
        print(f"  [{done:>3}/{len(sites)}] {mark} {domain:<30} {detail}", flush=True)

    await asyncio.gather(*(work(r, d) for r, d in sites))

    passed = sum(1 for r in rows if r["ok"])
    ours = [r for r in rows if r.get("self_inflicted")]
    print(f"\n{passed}/{len(rows)} read ({passed * 100 // max(len(rows), 1)}%)")
    print(
        f"{len(rows) - passed} failed, of which {len(ours)} are OURS (a plain client got the page)"
    )
    if ours:
        print("\nOurs, worst first:")
        for r in sorted(ours, key=lambda r: -(r.get("control_words") or 0))[:25]:
            print(
                f"  {r['domain']:<30} {str(r.get('error_code')):<18} "
                f"{str(r.get('signal') or ''):<24} control={r.get('control_words')}w"
            )
    if run_id:
        await repo.finish_benchmark_run(run_id)
        print(f"\nstored as {run_id} — read it back with --report {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
