#!/usr/bin/env python3
"""Load test at expected peak (10-build-plan.md, Phase 6).

07-orchestration.md sizes the HTTP worker pool at 50-200 concurrent. This
drives the real pipeline — fetch, block detection, extraction, all of it — at
that concurrency and reports what actually happens.

Two design decisions worth stating, because both are ways a load test can
report a confident and meaningless number:

**It calibrates the target first.** A load test that measures its own harness
is the standard failure. So the run starts by hitting the local target server
directly with no engine in the path, establishing the ceiling. If the engine's
throughput lands near that ceiling, the harness is the bottleneck and the
engine number is a floor, not a measurement. The report says so rather than
leaving you to work it out.

**It serves real HTML.** Extraction is most of the per-page cost, and a target
returning `<html>ok</html>` would skip it entirely and report a throughput the
engine can never reach on anything real. The fixture is a full article page.

    python tools/load_test.py                     # 200 concurrent, the spec's peak
    python tools/load_test.py --concurrency 50    # the bottom of the range
    python tools/load_test.py --requests 2000
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import os
import resource
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

# Peak from 07-orchestration.md's queue table.
DEFAULT_CONCURRENCY = 200
DEFAULT_REQUESTS = 1_000

# At peak the worker pool's concurrency is spread across many hosts — the
# politeness gate guarantees it. Pinning every request to one domain measures
# that gate rather than the engine. See spread_across_domains().
#
# Two separate limits had to be cleared before this measured the engine.
#
# Concurrency: each domain allows `politeness_default_concurrency` (2) in
# flight, so 100 domains is exactly 200 slots — at 200 concurrent that is
# saturation with nothing spare, and 80% of requests timed out waiting.
#
# Crawl delay: the gate ALSO spaces consecutive requests to the same domain.
# Cycling through 400 domains for 1000 requests still hit each one twice
# inside its delay window, so 40% blocked with slots free.
#
# Hence one domain per request by default. That is the honest model of the
# spec's peak anyway: 200 concurrent workers pulling from a large frontier are
# working on 200 different hosts, because the politeness gate is what makes
# them. Pass --domains explicitly to measure the gate instead.

# A run that fails more than this is not a performance measurement, it is a
# bug report — the numbers from it would be meaningless anyway.
MAX_ERROR_RATE = 0.01


# --------------------------------------------------------------------------
# Target server
# --------------------------------------------------------------------------


def page_html(index: int) -> bytes:
    """A realistic article, so extraction does the work it will do in
    production. Varied per page so nothing can be trivially cached."""
    from engine.tests.fixtures.builders import article_html

    html = article_html()
    return html.replace("</body>", f"<!-- page {index} --></body>").encode()


class TargetServer:
    """A minimal HTTP/1.1 server on asyncio.

    Deliberately not http.server: a thread-per-connection server at 200
    concurrent becomes the bottleneck, and then the run measures Python's GIL
    rather than the engine.
    """

    def __init__(self, latency_ms: int = 0) -> None:
        self.latency_s = latency_ms / 1000
        self.served = 0
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                request = await reader.readuntil(b"\r\n\r\n")
                if not request:
                    break
                if self.latency_s:
                    await asyncio.sleep(self.latency_s)

                self.served += 1
                body = page_html(self.served)
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/html; charset=utf-8\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"Connection: keep-alive\r\n\r\n" + body
                )
                await writer.drain()
        except (
            asyncio.IncompleteReadError,
            ConnectionResetError,
            BrokenPipeError,
            asyncio.LimitOverrunError,
        ):
            pass
        finally:
            writer.close()


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------


@dataclass
class Results:
    label: str
    latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0
    wall_s: float = 0.0
    confidences: list[float] = field(default_factory=list)
    # Counted by reason. The first run of this tool failed 100% and reported
    # only the zero — which told you something was wrong and nothing about
    # what. A load test that hides why it failed is a load test you cannot use.
    failures: Counter[str] = field(default_factory=Counter)

    def record_failure(self, exc: BaseException) -> None:
        self.errors += 1
        self.failures[f"{type(exc).__name__}: {str(exc)[:80]}"] += 1

    @property
    def completed(self) -> int:
        return len(self.latencies_ms)

    @property
    def throughput(self) -> float:
        return self.completed / self.wall_s if self.wall_s else 0.0

    @property
    def error_rate(self) -> float:
        total = self.completed + self.errors
        return self.errors / total if total else 0.0

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        index = min(int(len(ordered) * p), len(ordered) - 1)
        return ordered[index]

    def report(self) -> str:
        lines = [
            f"  requests      {self.completed} ok, {self.errors} failed "
            f"({self.error_rate * 100:.2f}%)",
            f"  throughput    {self.throughput:.1f} pages/sec",
            f"  latency p50   {self.percentile(0.50):.0f}ms",
            f"  latency p95   {self.percentile(0.95):.0f}ms",
            f"  latency p99   {self.percentile(0.99):.0f}ms",
            f"  latency max   {max(self.latencies_ms, default=0):.0f}ms",
        ]
        for reason, count in self.failures.most_common(3):
            lines.append(f"  failure       {count}x {reason}")
        if self.confidences:
            lines.append(
                f"  extraction    mean confidence "
                f"{statistics.fmean(self.confidences):.3f} "
                f"over {len(self.confidences)} pages"
            )
        return "\n".join(lines)


def peak_rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KB, macOS reports bytes.
    return usage / 1024 / 1024 if sys.platform == "darwin" else usage / 1024


async def calibrate(port: int, requests: int, concurrency: int) -> Results:
    """Hit the target directly. This is the ceiling the engine cannot beat."""
    import httpx

    results = Results("target server (no engine)")
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=concurrency), timeout=30
    ) as client:

        async def one(index: int) -> None:
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/page/{index}")
                    response.raise_for_status()
                    results.latencies_ms.append((time.perf_counter() - started) * 1000)
                except Exception as exc:
                    results.record_failure(exc)

        started = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(requests)))
        results.wall_s = time.perf_counter() - started

    return results


def allow_loopback_target() -> None:
    """Let this process fetch 127.0.0.1, and only this process.

    The SSRF guard correctly refuses loopback and private addresses — it is
    what stops a caller-supplied URL reaching our own metadata service — and
    the first run of this tool failed all 200 requests on exactly that.

    It is patched here, in the load-test process, rather than exposed as a
    setting. An `ENGINE_ALLOW_INTERNAL_ADDRESSES` env var would be read by
    every deployment, and a flag that disables SSRF protection is one someone
    eventually sets in production to make a staging box work. There must be no
    such flag to set. The guard stays absolute in the shipped engine; the test
    harness reaches around it in its own memory.
    """
    from engine.core import ssrf

    original = ssrf.resolve_and_validate

    async def permit_loopback(url: str) -> object:
        from urllib.parse import urlsplit

        if urlsplit(url).hostname in {"127.0.0.1", "localhost"}:
            parts = urlsplit(url)
            return ssrf.ResolvedTarget(
                url=url,
                host="127.0.0.1",
                port=parts.port or 80,
                scheme=parts.scheme,
                addresses=("127.0.0.1",),
            )
        return await original(url)

    ssrf.resolve_and_validate = permit_loopback  # type: ignore[assignment]

    # scrape_service binds the name at import time, so patching the module
    # alone works only if this runs first. Rebind there too rather than relying
    # on import order — an order-dependent patch fails silently and looks like
    # the guard rejecting a legitimate target.
    from engine.core import scrape_service

    scrape_service.resolve_and_validate = permit_loopback  # type: ignore[assignment]

    # Whoever reads this output should know the guard was not in the path.
    print("  (SSRF guard bypassed for 127.0.0.1 in this process only)\n")


def spread_across_domains(count: int) -> None:
    """Make each request look like a different domain to the politeness gate.

    The first honest run of this tool managed 1 page/sec and failed 79% of
    requests with "could not acquire a politeness slot". Nothing was broken:
    every request targeted 127.0.0.1, and per-domain concurrency defaults to
    2. The engine was throttling itself, correctly.

    That matters for what this tool is measuring. The spec's 50-200 figure is
    the size of the worker POOL, and at that concurrency the work is spread
    across many hosts by definition — one domain never sees 200 parallel
    requests from us, because the politeness gate exists precisely to stop
    that. A load test that pins everything to one host measures the gate, not
    the engine, and reports a throughput ceiling that no real workload hits.

    So the domain each request is charged against is varied. The gate still
    runs, and its cost is still in the numbers; it is simply asked the
    question it would be asked in production.
    """
    from engine.core import politeness

    original = politeness.PolitenessGate.acquire
    counter = itertools.count()

    # Unique per run. Without this the second run of the tool reuses the first
    # run's simulated domains, whose crawl-delay keys are still live in Redis,
    # and it reports a collapse in throughput that is entirely an artefact of
    # having been run twice. Two identical invocations must give the same
    # answer, or the numbers cannot be compared to anything.
    run_token = os.getpid() ^ time.time_ns() & 0xFFFFFF

    async def acquire_spread(self: object, domain: str, **kwargs: object) -> object:
        simulated = f"h{next(counter) % count}r{run_token}.{domain}"
        return await original(self, simulated, **kwargs)  # type: ignore[arg-type]

    politeness.PolitenessGate.acquire = acquire_spread  # type: ignore[assignment,method-assign]


async def drive_engine(port: int, requests: int, concurrency: int) -> Results:
    """The real pipeline: fetch, block detection, extraction."""
    from engine.core.fetch.tier0_http import HttpFetcher
    from engine.core.models import ScrapeOptions, Tier
    from engine.core.scrape_service import ScrapeService

    fetcher = HttpFetcher()
    # persist=False: this measures the engine, not Postgres. Storage
    # throughput is a separate question with a separate answer.
    service = ScrapeService({Tier.HTTP: fetcher}, persist=False)

    results = Results("engine (fetch + detect + extract)")
    semaphore = asyncio.Semaphore(concurrency)
    # maxAge=0 defeats the cache — otherwise this measures cache lookups.
    options = ScrapeOptions(maxAge=0, storeInCache=False)

    async def one(index: int) -> None:
        async with semaphore:
            started = time.perf_counter()
            try:
                outcome = await service.scrape(f"http://127.0.0.1:{port}/page/{index}", options)
                results.latencies_ms.append((time.perf_counter() - started) * 1000)
                confidence = getattr(outcome.data.metadata, "extractionConfidence", None)
                if isinstance(confidence, (int, float)):
                    results.confidences.append(float(confidence))
            except Exception as exc:
                results.record_failure(exc)

    started = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(requests)))
    results.wall_s = time.perf_counter() - started

    if hasattr(fetcher, "aclose"):
        await fetcher.aclose()
    return results


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------


async def run(concurrency: int, requests: int, latency_ms: int, domains: int) -> int:
    from engine.core.politeness import close_redis

    print(f"Load test — {concurrency} concurrent, {requests} requests")
    print(f"Spread across {domains} simulated domains")
    print(f"Target latency {latency_ms}ms per page\n")

    allow_loopback_target()
    spread_across_domains(domains)
    server = TargetServer(latency_ms)
    await server.start()
    rss_before = peak_rss_mb()

    try:
        ceiling = await calibrate(server.port, requests, concurrency)
        print(f"{ceiling.label}:")
        print(ceiling.report())
        print()

        engine = await drive_engine(server.port, requests, concurrency)
        print(f"{engine.label}:")
        print(engine.report())
        print(f"  peak RSS      {peak_rss_mb():.0f}MB (was {rss_before:.0f}MB)")
        print()
    finally:
        await server.stop()
        # The politeness gate holds a Redis connection open. A failure to
        # close it must not mask the results we came here for.
        with contextlib.suppress(Exception):
            await close_redis()

    # ----------------------------------------------------------------------
    # Verdict
    # ----------------------------------------------------------------------

    problems: list[str] = []

    politeness_failures = sum(
        count for reason, count in engine.failures.items() if "politeness" in reason
    )
    if politeness_failures > engine.completed * 0.05:
        problems.append(
            f"{politeness_failures} requests were refused by the politeness gate, "
            f"not by the engine. Raise --domains above "
            f"{concurrency // 2} so the gate has headroom, or this measures the "
            f"gate rather than the pipeline"
        )

    if engine.error_rate > MAX_ERROR_RATE:
        problems.append(
            f"error rate {engine.error_rate * 100:.1f}% exceeds "
            f"{MAX_ERROR_RATE * 100:.0f}% — these numbers describe a bug, not performance"
        )

    if ceiling.throughput and engine.throughput > ceiling.throughput * 0.9:
        problems.append(
            f"engine throughput ({engine.throughput:.0f}/s) is within 10% of the "
            f"harness ceiling ({ceiling.throughput:.0f}/s). The target server is "
            f"the bottleneck, so this is a FLOOR, not a measurement — rerun on "
            f"more cores or with a lighter target before quoting it"
        )

    overhead = engine.percentile(0.50) - ceiling.percentile(0.50) if ceiling.latencies_ms else 0
    print(f"Engine overhead at p50: {overhead:.0f}ms per page above the bare fetch.")
    print(
        f"Sustained {engine.throughput:.0f} pages/sec at {concurrency} concurrent, "
        f"{peak_rss_mb():.0f}MB peak RSS."
    )

    if problems:
        print("\nCAVEATS:", file=sys.stderr)
        for problem in problems:
            print(f"  ! {problem}", file=sys.stderr)
        return 1

    print("\nNo caveats: the engine, not the harness, was the limiting factor.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Load test at expected peak")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--requests", type=int, default=DEFAULT_REQUESTS)
    parser.add_argument(
        "--domains",
        type=int,
        default=0,
        help="distinct domains to spread across (default: enough to leave the "
        "politeness gate headroom); 1 measures the gate itself",
    )
    parser.add_argument(
        "--latency-ms",
        type=int,
        default=0,
        help="artificial per-page delay at the target, to model a slow site",
    )
    args = parser.parse_args()

    domains = args.domains or args.requests
    return asyncio.run(run(args.concurrency, args.requests, args.latency_ms, domains))


if __name__ == "__main__":
    raise SystemExit(main())
