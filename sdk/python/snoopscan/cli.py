"""The `snoopscan` command line.

One vocabulary across the REST API, the SDK and this: `scrape` here is
`/v1/scrape` there is `client.scrape()` in Python. Learning one surface teaches
the other two, and a docs example pastes into any of them.

Two things this deliberately does that a wrapper usually does not:

**It tells you what it cost and how it got there.** Every result prints the
tier that answered and the credits spent. A scraper that hides whether a page
came from a plain HTTP request or forty seconds of stealth browser is hiding
the only number that predicts the bill.

**It distinguishes the ways a page can fail.** BLOCKED, THIN, TARGET_ERROR and
ROBOTS_DENIED are different problems with different fixes, and collapsing them
into "failed" is how a formatting bug gets mistaken for a WAF.

Standard library only: argparse, not click. A CLI that drags a dependency tree
into every project that installs the SDK is a tax on people who only wanted the
client.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import webbrowser
from typing import Any

import httpx

from . import _update
from . import config as user_config
from .client import DEFAULT_BASE_URL, SnoopScan, SnoopScanError

ENV_KEY = "SNOOPSCAN_API_KEY"
ENV_URL = "SNOOPSCAN_BASE_URL"
# Where accounts live. The API is on its own host; signing in happens on the site.
ENV_ACCOUNT = "SNOOPSCAN_ACCOUNT_URL"
DEFAULT_ACCOUNT_URL = "https://snoopscan.com"

NO_KEY = (
    "No API key yet. Get a free one (no card needed):\n"
    "  snoopscan login          opens your browser to sign in or sign up, and saves the key\n"
    "Or paste one from https://snoopscan.com/app/keys:\n"
    "  snoopscan config set api_key <key>      (or set $SNOOPSCAN_API_KEY)"
)

# Exit codes, so a shell script can branch. 1 is "we failed", 2 is "the target
# refused us" — different problems, and a caller retrying the second one
# forever is exactly the waste this separation prevents.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def _emit(value: Any, args: argparse.Namespace) -> None:
    """Write the result, to a file when asked, to stdout otherwise."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, indent=2 if args.pretty else None, ensure_ascii=False)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
        _note(f"written to {args.output} ({len(text):,} chars)", args)
        return
    print(text)


def _note(message: str, args: argparse.Namespace) -> None:
    """A line for the operator, on stderr so it never pollutes piped output."""
    if not args.quiet:
        print(message, file=sys.stderr)


def _receipt(payload: dict[str, Any], args: argparse.Namespace) -> None:
    """How the page was got, and what it cost.

    On stderr deliberately: `snoopscan scrape url > page.md` must produce a
    clean file, and the receipt is for the human watching, not the pipe.
    """
    if args.quiet or not isinstance(payload, dict):
        return
    cost = payload.get("cost") or {}
    meta = payload.get("metadata") or {}
    bits = []
    if tier := (cost.get("tier") or meta.get("tier")):
        bits.append(f"tier={tier}")
    if (credits := cost.get("credits")) is not None:
        bits.append(f"credits={credits}")
    if (ms := cost.get("durationMs") or cost.get("duration_ms")) is not None:
        bits.append(f"{int(ms) / 1000:.1f}s")
    if bits:
        print("  " + "  ".join(bits), file=sys.stderr)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _options(args: argparse.Namespace) -> dict[str, Any]:
    """Shared scrape-ish options, omitting anything the caller left alone so
    the API applies its own defaults rather than ours."""
    out: dict[str, Any] = {}
    if args.tier:
        out["tier"] = args.tier
    if args.timeout:
        out["timeout"] = args.timeout
    if getattr(args, "max_age", None) is not None:
        out["maxAge"] = args.max_age
    if getattr(args, "formats", None):
        out["formats"] = args.formats.split(",")
    return out


def _as_text(value: Any) -> str:
    """One format, readable: links one per line, text as-is, the rest as JSON."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(item.get("url") or item.get("href") or item)
            if isinstance(item, dict)
            else str(item)
            for item in value
        )
    return json.dumps(value, indent=2, ensure_ascii=False)


def _readable(raw: dict[str, Any], formats: list[str]) -> str:
    """What was asked for, not always the markdown.

    `--formats links,rawHtml` used to print the markdown — which was not
    requested, so it was empty — and exit 0. Nothing on screen read as "this
    page has no links", and a check built on it reported live links as gone.
    """
    parts = [(name, _as_text(raw.get(name))) for name in formats]
    if len(parts) == 1:
        return parts[0][1]
    return "\n\n".join(f"--- {name} ---\n{text}" for name, text in parts)


def cmd_scrape(client: SnoopScan, args: argparse.Namespace) -> int:
    doc = client.scrape(args.url, **_options(args))
    _receipt(doc.raw, args)
    if doc.is_suspect and not args.quiet:
        # Worth saying out loud: a low-confidence extraction reads like a
        # normal result and is the shape a menu bar or a nav shell arrives in.
        print(f"  low confidence ({doc.extraction_confidence:.2f}) — check it", file=sys.stderr)
    if args.json:
        _emit(doc.raw, args)
        return EXIT_OK
    formats = _options(args).get("formats") or ["markdown"]
    # The typed fields first, then anything else the payload carries.
    source = {
        **doc.raw,
        "markdown": doc.markdown or doc.raw.get("markdown"),
        "html": doc.html or doc.raw.get("html"),
        "rawHtml": doc.raw_html or doc.raw.get("rawHtml"),
        "links": doc.links or doc.raw.get("links"),
    }
    if all(not source.get(name) for name in formats):
        # Loud even under --quiet: an empty answer must never pass for "none".
        wanted = ", ".join(formats)
        print(
            f"  nothing came back for {wanted}. Try --json for the whole response.", file=sys.stderr
        )
        return EXIT_ERROR
    _emit(_readable(source, formats), args)
    return EXIT_OK


def cmd_map(client: SnoopScan, args: argparse.Namespace) -> int:
    links = client.map(args.url, **_options(args))
    _note(f"  {len(links)} links", args)
    if args.json:
        _emit(links, args)
    else:
        _emit("\n".join(str(row.get("url", row)) for row in links), args)
    return EXIT_OK


def cmd_search(client: SnoopScan, args: argparse.Namespace) -> int:
    body: dict[str, Any] = {"limit": args.limit}
    if args.scrape:
        # The API takes ScrapeOptions here, not a bare flag — an empty object
        # opts in with its own defaults. `scrapeResults: true` was rejected
        # outright by the strict request model (extra="forbid"), so
        # `--scrape` never actually worked.
        body["scrapeOptions"] = {}
    data = client.search(args.query, **body)
    results = data.get("results", []) if isinstance(data, dict) else data
    # Which provider answered. The ladder falling through to a weaker engine is
    # correct behaviour and invisible unless it is printed — a search that
    # silently degrades returns a worse source mix at the same speed.
    if isinstance(data, dict) and (provider := data.get("provider")):
        _note(f"  provider={provider}  results={len(results)}", args)
    if args.json:
        _emit(data, args)
    else:
        _emit("\n".join(f"{r.get('title', '')}\n  {r.get('url', '')}" for r in results), args)
    return EXIT_OK


def cmd_crawl(client: SnoopScan, args: argparse.Namespace) -> int:
    # A crawl takes page options under scrapeOptions: the API is strict, and
    # `--formats` sent at the top level was rejected on every call.
    page = _options(args)
    options: dict[str, Any] = {"scrapeOptions": page} if page else {}
    if args.limit:
        options["limit"] = args.limit
    if args.wait:
        docs = client.crawl_and_wait(args.url, max_wait=args.max_wait, **options)
        _note(f"  {len(docs)} pages", args)
        joined = "\n\n---\n\n".join(d.markdown or "" for d in docs)
        _emit([d.raw for d in docs] if args.json else joined, args)
        return EXIT_OK
    job = client.crawl(args.url, **options)
    _note(f"  job {job.id} started — snoopscan crawl-status {job.id}", args)
    _emit({"id": job.id, "status": job.status}, args)
    return EXIT_OK


def cmd_crawl_status(client: SnoopScan, args: argparse.Namespace) -> int:
    job = client.crawl_status(args.job_id)
    _emit(
        {
            "id": job.id,
            "status": job.status,
            "total": job.total,
            "completed": job.completed,
            "failed": job.failed,
        },
        args,
    )
    return EXIT_OK


def cmd_extract(client: SnoopScan, args: argparse.Namespace) -> int:
    schema = (
        json.loads(args.schema)
        if args.schema.strip().startswith("{")
        else json.loads(open(args.schema, encoding="utf-8").read())
    )
    rows = client.extract(args.urls, schema, prompt=args.prompt)
    _emit(rows, args)
    return EXIT_OK


def cmd_parse(client: SnoopScan, args: argparse.Namespace) -> int:
    if args.target.startswith(("http://", "https://")):
        data = client.parse(url=args.target)
    else:
        # The file as it is on disk. Reading it as text first broke every PDF.
        data = client.parse(path=args.target)
    _receipt(data, args)
    _emit(data if args.json else data.get("markdown", ""), args)
    return EXIT_OK


def _listing(rows: list[dict[str, Any]], title_keys: tuple[str, ...]) -> str:
    """A readable line per item.

    The default output of a catalogue command must not be the catalogue. One
    blog returned 4.2 MB of post bodies to a terminal — technically the right
    data, unusably delivered. Full fidelity is one flag away (`--json`), and
    `-o` writes it to a file where that size belongs.
    """
    lines = []
    for row in rows:
        title = next((str(row[k]) for k in title_keys if row.get(k)), "(untitled)")
        url = row.get("url") or row.get("link") or ""
        price = row.get("price")
        suffix = f"  {price}" if price else ""
        lines.append(f"{title}{suffix}\n  {url}")
    return "\n".join(lines)


def cmd_products(client: SnoopScan, args: argparse.Namespace) -> int:
    data = client.products(args.url)
    products = data.get("products", [])
    _note(
        f"  platform={data.get('platform')}  total={data.get('total')}  returned={len(products)}",
        args,
    )
    _emit(data if args.json else _listing(products, ("title", "name")), args)
    return EXIT_OK


def cmd_posts(client: SnoopScan, args: argparse.Namespace) -> int:
    data = client.posts(args.url)
    posts = data.get("posts", [])
    _note(
        f"  platform={data.get('platform')}  source={data.get('source')}  returned={len(posts)}",
        args,
    )
    _emit(data if args.json else _listing(posts, ("title", "name")), args)
    return EXIT_OK


def cmd_company(client: SnoopScan, args: argparse.Namespace) -> int:
    data = client.company(args.url, contacts=not args.no_contacts)
    company = data.get("company") or {}
    contacts = data.get("contacts") or {}
    _note(
        f"  name={company.get('name')!r}  emails={len(contacts.get('emails') or [])}"
        f"  pagesRead={data.get('pagesRead')}",
        args,
    )
    _emit(data, args)
    return EXIT_OK


def cmd_domain(client: SnoopScan, args: argparse.Namespace) -> int:
    data = client.domain(
        args.domain,
        registration=not args.no_registration,
        dns=not args.no_dns,
        backlinks=not args.no_backlinks,
    )
    _note(f"  domain={data.get('domain')}", args)
    _emit(data, args)
    return EXIT_OK


def cmd_monitor(client: SnoopScan, args: argparse.Namespace) -> int:
    if args.action == "list":
        _emit(client.monitors(), args)
    elif args.action == "get":
        _emit(client.monitor(args.monitor_id), args)
    elif args.action == "run":
        _emit(client.run_monitor(args.monitor_id), args)
    elif args.action == "checks":
        _emit(client.monitor_checks(args.monitor_id), args)
    elif args.action == "delete":
        client.delete_monitor(args.monitor_id)
        _note(f"  deleted {args.monitor_id}", args)
    elif args.action == "create":
        _emit(
            client.create_monitor(
                args.name, args.urls.split(","), intervalMinutes=args.interval, goal=args.goal
            ),
            args,
        )
    return EXIT_OK


def cmd_config(client: SnoopScan, args: argparse.Namespace) -> int:
    """Read and write the settings file, so a key survives the shell."""
    path = user_config.config_path()

    if args.action == "path":
        print(path)
        return EXIT_OK

    if args.action == "show":
        stored = user_config.load()
        print(f"  file     {path}{'' if path.is_file() else '  (not created yet)'}")
        for key in user_config.KNOWN:
            value = stored.get(key, "")
            shown = user_config.redact(value) if key.endswith("key") else value
            print(f"  {key:9s}{shown or '—'}")
        # Where a value is coming from matters more than what it is: a stored
        # key that an environment variable is quietly overriding is a long
        # afternoon otherwise.
        for env, key in ((ENV_KEY, "api_key"), (ENV_URL, "base_url")):
            if os.environ.get(env):
                print(f"  NOTE     ${env} is set and overrides {key} above")
        return EXIT_OK

    if args.action == "get":
        # Prints the raw value, so an agent can put the real key into an MCP
        # config or .env itself rather than hand anyone a placeholder.
        if args.name not in user_config.KNOWN:
            print(f"Usage: snoopscan config get <{'|'.join(user_config.KNOWN)}>", file=sys.stderr)
            return EXIT_ERROR
        value = (os.environ.get(ENV_KEY) if args.name == "api_key" else None) or (
            user_config.load().get(args.name, "")
        )
        if not value:
            print(NO_KEY if args.name == "api_key" else f"{args.name} is not set.", file=sys.stderr)
            return EXIT_ERROR
        print(value)
        return EXIT_OK

    if args.action == "set":
        if not args.value:
            print("Usage: snoopscan config set <api_key|base_url> <value>", file=sys.stderr)
            return EXIT_ERROR
        if args.name not in user_config.KNOWN:
            print(
                f"Unknown setting {args.name!r}. Known: {', '.join(user_config.KNOWN)}",
                file=sys.stderr,
            )
            return EXIT_ERROR
        written = user_config.save({args.name: args.value})
        shown = user_config.redact(args.value) if args.name.endswith("key") else args.value
        print(f"  {args.name} = {shown}")
        print(f"  saved to {written} (0600)")
        return EXIT_OK

    print("Usage: snoopscan config <show|get|set|path>", file=sys.stderr)
    return EXIT_ERROR


def cmd_login(client: SnoopScan, args: argparse.Namespace) -> int:
    """Sign in through the browser and save a fresh key: nothing to copy.

    The device-code pattern: ask the site for a pair of codes, open the
    browser at the short one, and poll with the long one until the person
    approves. The short code is printed here and shown on the approve page,
    so they can see the request they approve is this one.
    """
    account = (args.account_url or os.environ.get(ENV_ACCOUNT) or DEFAULT_ACCOUNT_URL).rstrip("/")
    try:
        with httpx.Client(base_url=account, timeout=20.0) as http:
            start = http.post("/api/cli/login/start", json={"host": socket.gethostname()})
            if start.status_code == 429:
                print(
                    "Too many login attempts from here. Wait a minute and try again.",
                    file=sys.stderr,
                )
                return EXIT_ERROR
            start.raise_for_status()
            data = start.json()["data"]
            # flush: an agent runs this with its output piped, and a buffered link never
            # reaches it until the login is over — too late to pass on.
            print("Sign in to SnoopScan in your browser to connect this terminal.", flush=True)
            print(
                f"  Code: {data['userCode']}   (check it matches the one on the page)", flush=True
            )
            print(f"  Link: {data['verifyUrl']}", flush=True)
            if not args.no_browser and not webbrowser.open(data["verifyUrl"]):
                print("  Open the link above in any browser.", flush=True)
            deadline = time.monotonic() + int(data.get("expiresIn", 600))
            interval = max(1, int(data.get("interval", 2)))
            print("Waiting for you to approve…", flush=True)
            while time.monotonic() < deadline:
                time.sleep(interval)
                polled = http.post("/api/cli/login/poll", json={"deviceCode": data["deviceCode"]})
                if polled.status_code == 429:
                    interval += 1
                    continue
                polled.raise_for_status()
                state = polled.json()["data"]
                if state.get("status") == "approved" and state.get("apiKey"):
                    written = user_config.save({"api_key": state["apiKey"]})
                    print(f"Logged in. Key saved to {written} (0600).")
                    print("Next: snoopscan status")
                    return EXIT_OK
                if state.get("status") == "denied":
                    print("Cancelled in the browser. Nothing was saved.", file=sys.stderr)
                    return EXIT_ERROR
                if state.get("status") == "expired":
                    break
    except httpx.HTTPError as exc:
        print(f"Could not reach {account}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nStopped. Nothing was saved.", file=sys.stderr)
        return EXIT_ERROR
    print("The login link expired. Run snoopscan login again.", file=sys.stderr)
    return EXIT_ERROR


def cmd_doctor(client: SnoopScan, args: argparse.Namespace) -> int:
    """Check everything a first run depends on, and say how to fix what is wrong.

    One command an agent can run and read, instead of guessing at an install:
    this tool's version against the latest, the Python it runs on, the config
    file and its permissions, whether a key is set and whether the API accepts
    it (on a free call), and whether the engine is up.
    """
    import platform

    from . import __version__

    ok = True

    def line(label: str, text: str, good: bool = True) -> None:
        nonlocal ok
        ok = ok and good
        print(f"  {'ok ' if good else 'FIX'}  {label:10s}{text}")

    latest = _update.latest_version(force=True)
    if latest and _update.is_newer(latest, __version__):
        line(
            "snoopscan",
            f"{__version__}, and {latest} is out. Update: pipx upgrade snoopscan "
            "(or uv tool upgrade snoopscan, or python3 -m pip install -U snoopscan)",
            False,
        )
    else:
        line(
            "snoopscan",
            f"{__version__}" + (" (the latest)" if latest else " (could not check PyPI)"),
        )

    py = platform.python_version()
    line("python", f"{py}", sys.version_info >= (3, 10))

    path = user_config.config_path()
    if path.is_file():
        mode = path.stat().st_mode & 0o777
        line("config", f"{path}", mode & 0o077 == 0)
        if mode & 0o077:
            print(f"            readable by others ({oct(mode)}). Fix: chmod 600 {path}")
    else:
        line("config", f"{path} (not created yet; `snoopscan login` makes it)")

    key = args.api_key or os.environ.get(ENV_KEY) or user_config.load().get("api_key", "")
    source = (
        "--api-key" if args.api_key else ("$" + ENV_KEY if os.environ.get(ENV_KEY) else "config")
    )
    if not key:
        line("key", "not set. Fix: snoopscan login", False)
    else:
        try:
            r = httpx.get(
                f"{client.base_url}/v1/templates",
                headers={"Authorization": f"Bearer {key.strip()}"},
                timeout=10,
            )
            if r.status_code == 200:
                line("key", f"{user_config.redact(key)} from {source}, accepted")
            elif r.status_code == 401:
                line(
                    "key",
                    f"{user_config.redact(key)} from {source} is not recognised. "
                    "Fix: snoopscan login",
                    False,
                )
            else:
                line("key", f"could not check (HTTP {r.status_code})", False)
        except httpx.HTTPError as exc:
            line("key", f"could not reach {client.base_url} ({type(exc).__name__})", False)

    try:
        health = httpx.get(f"{client.base_url}/health", timeout=10).json()
        line("engine", f"{health.get('status', '?')} at {client.base_url}")
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        line("engine", f"unreachable at {client.base_url} ({type(exc).__name__})", False)

    print("  All good." if ok else "  Fix the lines marked FIX, then run: snoopscan doctor")
    return EXIT_OK if ok else EXIT_ERROR


def cmd_status(client: SnoopScan, args: argparse.Namespace) -> int:
    """Whether this is configured AND whether the engine can do the hard work.

    The second half matters: Camoufox's browser build lives in an OS cache
    directory that cleaning tools empty, and losing it drops the only rungs
    that pass a hard anti-bot check. The engine then answers BLOCKED for those
    domains, which reads as the targets refusing us rather than as a missing
    dependency. One line here beats an hour of misdirected debugging.
    """
    import httpx

    base = client.base_url
    print(f"  api      {base}")
    try:
        health = httpx.get(f"{base}/health", timeout=10).json()
        print(f"  engine   {health.get('status', '?')}  v{health.get('version', '?')}")
        tiers = health.get("tiers") or []
        deep = health.get("deepTiersAvailable")
        print(f"  tiers    {', '.join(tiers) if tiers else 'unknown'}")
        if deep is False:
            print("  WARNING  the deep rungs are missing — hard sites will report BLOCKED")
            print("           fix: python -m camoufox fetch")
        if degraded := health.get("searchProvidersDegraded"):
            print(f"  WARNING  search rungs degraded: {', '.join(degraded)}")
        if health.get("saturated"):
            print(f"  WARNING  engine saturated (loop lag {health.get('recentLagMs')}ms)")
    except Exception as exc:  # noqa: BLE001 - status must report, never raise
        print(f"  engine   unreachable ({type(exc).__name__})")
        return EXIT_ERROR
    return EXIT_OK


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # Global flags live on a PARENT parser so they work on either side of the
    # subcommand. argparse's default puts them before it only, and
    # `snoopscan scrape URL --json` — which is what everyone types — would fail
    # with "unrecognized arguments". A CLI that rejects the natural word order
    # is a CLI people stop using.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--api-key", help=f"defaults to ${ENV_KEY}")
    common.add_argument("--base-url", help=f"defaults to ${ENV_URL} or {DEFAULT_BASE_URL}")
    common.add_argument("-o", "--output", help="write to a file instead of stdout")
    common.add_argument("--json", action="store_true", help="full JSON, not just the content")
    common.add_argument("--pretty", action="store_true", help="indent the JSON")
    common.add_argument("-q", "--quiet", action="store_true", help="no receipts on stderr")

    # Flags belong AFTER the subcommand, as in git and docker: `snoopscan
    # scrape URL --json`. They are deliberately NOT on the top-level parser —
    # with `parents` on both, argparse re-applies the subparser's default and
    # silently discards a flag given before the verb, which is worse than
    # rejecting it.
    parser = argparse.ArgumentParser(
        prog="snoopscan",
        description="Scrape, crawl, search and extract the web. Same verbs as the API and the SDK.",
    )

    subs = parser.add_subparsers(
        dest="command", required=True, parser_class=argparse.ArgumentParser
    )

    def page_options(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--tier",
            choices=["http", "impersonate", "browser", "stealth", "stealth_hard", "mobile", "auto"],
        )
        sub.add_argument("--timeout", type=int, help="milliseconds")
        sub.add_argument("--max-age", type=int, help="accept a cached page this many ms old")
        sub.add_argument(
            "--formats", help="comma separated: markdown,html,rawHtml,links,screenshot"
        )

    s = subs.add_parser("scrape", parents=[common], help="get clean content from one URL")
    s.add_argument("url")
    page_options(s)
    s.set_defaults(func=cmd_scrape)

    s = subs.add_parser("crawl", parents=[common], help="crawl a site")
    s.add_argument("url")
    s.add_argument("--limit", type=int, help="maximum pages")
    s.add_argument("--wait", action="store_true", help="block until it finishes")
    s.add_argument("--max-wait", type=float, default=900.0)
    page_options(s)
    s.set_defaults(func=cmd_crawl)

    s = subs.add_parser("crawl-status", parents=[common], help="how a crawl is going")
    s.add_argument("job_id")
    s.set_defaults(func=cmd_crawl_status)

    s = subs.add_parser("map", parents=[common], help="discover URLs without fetching bodies")
    s.add_argument("url")
    page_options(s)
    s.set_defaults(func=cmd_map)

    s = subs.add_parser("search", parents=[common], help="search the web")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--scrape", action="store_true", help="fetch each result too")
    s.set_defaults(func=cmd_search)

    s = subs.add_parser("extract", parents=[common], help="structured data against a schema")
    s.add_argument("urls", nargs="+")
    s.add_argument("--schema", required=True, help="inline JSON or a path to a .json file")
    s.add_argument("--prompt", help="what to pull out, in words")
    s.set_defaults(func=cmd_extract)

    s = subs.add_parser(
        "parse", parents=[common], help="a document to markdown — PDF, DOCX, XLSX, HTML"
    )
    s.add_argument("target", help="a URL or a local file")
    s.set_defaults(func=cmd_parse)

    s = subs.add_parser(
        "products", parents=[common], help="a store's catalogue, from its own endpoint"
    )
    s.add_argument("url")
    s.set_defaults(func=cmd_products)

    s = subs.add_parser("posts", parents=[common], help="a site's posts, from its API or feed")
    s.add_argument("url")
    s.set_defaults(func=cmd_posts)

    s = subs.add_parser(
        "company", parents=[common], help="firmographics and contacts from a company's own site"
    )
    s.add_argument("url")
    s.add_argument(
        "--no-contacts", action="store_true", help="skip contact discovery, firmographics only"
    )
    s.set_defaults(func=cmd_company)

    s = subs.add_parser(
        "domain", parents=[common], help="registration, DNS and backlinks for a domain"
    )
    s.add_argument("domain")
    s.add_argument("--no-registration", action="store_true")
    s.add_argument("--no-dns", action="store_true")
    s.add_argument("--no-backlinks", action="store_true")
    s.set_defaults(func=cmd_domain)

    s = subs.add_parser("monitor", parents=[common], help="watch pages for changes")
    s.add_argument("action", choices=["list", "get", "run", "checks", "delete", "create"])
    s.add_argument("monitor_id", nargs="?")
    s.add_argument("--name")
    s.add_argument("--urls", help="comma separated")
    s.add_argument("--interval", type=int, default=60, help="minutes, minimum 5")
    s.add_argument("--goal", help="what change matters")
    s.set_defaults(func=cmd_monitor)

    s = subs.add_parser(
        "config", parents=[common], help="store the key and URL so every shell has them"
    )
    s.add_argument("action", choices=("show", "get", "set", "path"))
    s.add_argument("name", nargs="?", help="api_key or base_url")
    s.add_argument("value", nargs="?")
    s.set_defaults(func=cmd_config)

    s = subs.add_parser(
        "login", parents=[common], help="sign in through the browser and save a key (no copying)"
    )
    s.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    s.add_argument("--account-url", help=f"defaults to ${ENV_ACCOUNT} or {DEFAULT_ACCOUNT_URL}")
    s.set_defaults(func=cmd_login)

    s = subs.add_parser(
        "doctor", parents=[common], help="check the install, the key and the engine; say how to fix"
    )
    s.set_defaults(func=cmd_doctor)

    s = subs.add_parser(
        "status", parents=[common], help="is this configured, and can the engine do the hard work"
    )
    s.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Flag, then environment, then the stored file. The flag wins so a one-off
    # override works; the environment beats the file so CI can inject a key
    # without writing a credential to disk.
    stored = user_config.load()
    api_key = args.api_key or os.environ.get(ENV_KEY) or stored.get("api_key", "")
    base_url = (
        args.base_url or os.environ.get(ENV_URL) or stored.get("base_url") or DEFAULT_BASE_URL
    )
    if not api_key and args.command not in ("status", "config", "login", "doctor"):
        print(NO_KEY, file=sys.stderr)
        return EXIT_ERROR

    client = SnoopScan(api_key or "none", base_url=base_url)
    try:
        return int(args.func(client, args))
    except SnoopScanError as exc:
        # The distinction is the point. A target refusing us is not the same
        # problem as a thin page or a broken request, and a caller that retries
        # all three identically wastes money on the two that will never change.
        # `str(exc)` already carries the code, so printing it again reads
        # "BLOCKED: BLOCKED: ...".
        print(str(exc), file=sys.stderr)
        if exc.detail and not args.quiet:
            print(f"  detail: {json.dumps(exc.detail)[:300]}", file=sys.stderr)
        return EXIT_BLOCKED if exc.code in {"BLOCKED", "ROBOTS_DENIED"} else EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_ERROR
    finally:
        client.close()
        # After the command, on stderr, so it never lands in piped output.
        # `doctor` reports the version itself.
        if args.command != "doctor":
            from . import __version__

            if line := _update.notice(__version__):
                print(line, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
