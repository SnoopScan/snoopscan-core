"""Python client for the SnoopScan web scraping API.

Deliberately mirrors the shape of the established clients in this space —
`scrape`, `crawl`, `map`, `extract`, `search`, plus a blocking `crawl_and_wait`
— because the migration promise is that existing code changes a base URL and
keeps working. Method and option names are the API's names.

Written from our own OpenAPI surface, not from any other client's source
(constraint C1).

    from snoopscan import SnoopScan

    snoop = SnoopScan(api_key="sk_...")
    page = snoop.scrape("https://example.com")
    print(page.markdown)
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

# The hosted API, api.snoopscan.com — where the overwhelming majority of
# `pip install snoopscan` actually runs against. This defaulted to
# `http://localhost:8099` until 15 Sep 2026 on the theory that the engine is
# self-hosted and "a published client cannot guess where YOUR instance runs" —
# true for someone running this open-core engine themselves, but that is a
# small, technically sophisticated minority, and everyone else's very first
# call failed with a connection error before they got anywhere (measured: a
# real customer/agent research session hit exactly this, same day). A
# self-hoster sets SNOOPSCAN_BASE_URL=http://localhost:8099 — one env var,
# and precisely the audience capable of setting it; defaulting to their
# address instead broke the common case to save the rare one a single line.
#
# `SNOOP_BASE_URL` is the pre-rename spelling, still honoured so an existing
# deployment does not break. The CLI reads `SNOOPSCAN_BASE_URL`, and the two
# must agree — before this fix they briefly could not disagree on the *value*,
# only on the localhost default both shared.
DEFAULT_BASE_URL = (
    os.environ.get("SNOOPSCAN_BASE_URL")
    or os.environ.get("SNOOP_BASE_URL")
    or "https://api.snoopscan.com"
)
DEFAULT_TIMEOUT = 120.0

# `timeout` (milliseconds) in a request body is the ENGINE's own budget —
# how long it may keep trying tiers server-side. It has nothing to do with
# DEFAULT_TIMEOUT, the SDK's own HTTP client deadline for waiting on THAT
# response — two clocks that happened to agree by coincidence whenever a
# caller asked for exactly 120s. Ask for 120s (or more) and the two now
# raced: whichever fired first won, and half the time it was httpx's own
# ReadTimeout instead of the engine's clean JSON error, indistinguishable
# from a real network failure. Reported live: a 120-second scrape crashed
# with a raw httpx.ReadTimeout, and a 60-second retry got a real engine
# error — the SDK's own ceiling was never widened for either.
# 10s of margin covers connection setup and response transfer that sit
# outside the engine's own accounting.
_TIMEOUT_MARGIN_S = 10.0


def _http_timeout_for(body: dict[str, Any], client_default: float) -> float:
    engine_timeout_ms = body.get("timeout")
    if not isinstance(engine_timeout_ms, (int, float)):
        return client_default
    return max(client_default, engine_timeout_ms / 1000 + _TIMEOUT_MARGIN_S)


class SnoopScanError(Exception):
    """An error returned by the API.

    Carries the machine-readable code so callers can branch on it —
    `BLOCKED` and `TARGET_ERROR` mean different things and deserve different
    handling.
    """

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}

    @property
    def is_blocked(self) -> bool:
        return self.code == "BLOCKED"

    @property
    def is_target_error(self) -> bool:
        """The site responded, but not with the page — a genuine 404 or 5xx,
        not us being blocked. Retrying will not help."""
        return self.code == "TARGET_ERROR"

    @property
    def is_rate_limited(self) -> bool:
        return self.code == "RATE_LIMITED"


@dataclass
class Cost:
    """What the request actually cost. Failed requests carry no cost."""

    tier: str | None = None
    tiers_attempted: list[str] = field(default_factory=list)
    proxy_used: bool = False
    proxy_type: str | None = None
    proxy_bytes: int = 0
    browser_ms: int = 0
    extraction_path: str | None = None
    cached: bool = False


@dataclass
class Document:
    markdown: str | None = None
    html: str | None = None
    raw_html: str | None = None
    links: list[str] = field(default_factory=list)
    json_: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    cost: Cost = field(default_factory=Cost)
    # The payload as it arrived. Kept because the typed fields are a curated
    # view: anything the API adds later, and anything a caller wants to dump
    # verbatim, is otherwise lost the moment it is parsed.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str | None:
        return self.metadata.get("title")

    @property
    def url(self) -> str | None:
        return self.metadata.get("url")

    @property
    def page_type(self) -> str:
        return self.metadata.get("pageType", "unknown")

    @property
    def word_count(self) -> int:
        return int(self.metadata.get("wordCount", 0))

    @property
    def extraction_confidence(self) -> float:
        """0-1. Treat anything below 0.5 as suspect and cross-check it."""
        return float(self.metadata.get("extractionConfidence", 0.0))

    @property
    def is_suspect(self) -> bool:
        return self.extraction_confidence < 0.5

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> Document:
        return cls(
            markdown=data.get("markdown"),
            html=data.get("html"),
            raw_html=data.get("rawHtml"),
            links=data.get("links") or [],
            json_=data.get("json"),
            metadata=data.get("metadata") or {},
            cost=Cost(
                **{k: v for k, v in (data.get("cost") or {}).items() if k in Cost.__annotations__}
            ),
            raw=data,
        )


@dataclass
class CrawlJob:
    id: str
    status: str
    total: int = 0
    completed: int = 0
    failed: int = 0
    cost: dict[str, Any] = field(default_factory=dict)

    @property
    def finished(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")


class SnoopScan:
    """Synchronous client. See AsyncSnoopScan for the async form."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._default_timeout = timeout
        self._client = httpx.Client(
            timeout=timeout,
            # No client-wide Content-Type: httpx sets JSON for a JSON body and
            # the multipart boundary for an upload. A fixed JSON header here
            # overrode the boundary, so /v1/parse never saw its file.
            headers={"Authorization": f"Bearer {api_key}"},
        )

    # -- plumbing ---------------------------------------------------------

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        request_timeout = _http_timeout_for(body, self._default_timeout)
        return self._unwrap(
            self._client.post(f"{self.base_url}{path}", json=body, timeout=request_timeout)
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._unwrap(self._client.get(f"{self.base_url}{path}", params=params))

    def _delete(self, path: str) -> Any:
        return self._unwrap(self._client.delete(f"{self.base_url}{path}"))

    @staticmethod
    def _unwrap(response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except ValueError:
            # Never leak an httpx exception for an API-level failure: callers
            # branch on `SnoopScanError.code`, and a raw HTTPStatusError
            # would bypass that entirely. A non-JSON body means something in
            # front of the API answered — a proxy, a load balancer, an nginx
            # error page — so it is reported as INTERNAL with the status.
            raise SnoopScanError(
                "INTERNAL",
                f"Non-JSON response from the API (HTTP {response.status_code})",
                {"status_code": response.status_code},
            ) from None

        if not payload.get("success", False):
            error = payload.get("error") or {}
            raise SnoopScanError(
                error.get("code", "INTERNAL"),
                error.get("message", "Unknown error"),
                error.get("detail"),
            )
        return payload.get("data")

    # -- endpoints --------------------------------------------------------

    def scrape(self, url: str, **options: Any) -> Document:
        """Fetch and extract one URL."""
        return Document.from_payload(self._post("/v1/scrape", {"url": url, **options}))

    def crawl(self, url: str, **options: Any) -> CrawlJob:
        """Start a crawl. Returns immediately with a job id."""
        data = self._post("/v1/crawl", {"url": url, **options})
        return CrawlJob(id=data["id"], status=data["status"])

    def crawl_status(self, job_id: str) -> CrawlJob:
        data = self._get(f"/v1/crawl/{job_id}")
        return CrawlJob(
            id=data["id"],
            status=data["status"],
            total=data.get("total", 0),
            completed=data.get("completed", 0),
            failed=data.get("failed", 0),
            cost=data.get("cost") or {},
        )

    def crawl_pages(
        self, job_id: str, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[Document], str | None]:
        """One page of results, plus the cursor for the next.

        Results are paginated rather than inlined because a 10,000-page crawl
        must not arrive as a single JSON body.
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = self._get(f"/v1/crawl/{job_id}/pages", params)
        documents = [Document.from_payload(row) for row in data.get("pages", [])]
        next_link = data.get("next")
        next_cursor = next_link.split("cursor=")[-1] if next_link else None
        return documents, next_cursor

    def crawl_errors(self, job_id: str) -> list[dict[str, Any]]:
        return self._get(f"/v1/crawl/{job_id}/errors").get("errors", [])

    def cancel_crawl(self, job_id: str) -> CrawlJob:
        data = self._client.delete(f"{self.base_url}/v1/crawl/{job_id}")
        payload = self._unwrap(data)
        return CrawlJob(id=payload["id"], status=payload["status"])

    def crawl_and_wait(
        self,
        url: str,
        poll_interval: float = 3.0,
        max_wait: float = 900.0,
        **options: Any,
    ) -> list[Document]:
        """Start a crawl and block until it finishes, then return every page.

        Convenience only. For anything large, start the crawl and read pages
        as they arrive rather than holding the whole result in memory.
        """
        job = self.crawl(url, **options)
        deadline = time.monotonic() + max_wait

        while time.monotonic() < deadline:
            current = self.crawl_status(job.id)
            if current.finished:
                break
            time.sleep(poll_interval)
        else:
            raise SnoopScanError("TIMEOUT", f"Crawl {job.id} did not finish within {max_wait:.0f}s")

        documents: list[Document] = []
        cursor: str | None = None
        while True:
            page, cursor = self.crawl_pages(job.id, cursor)
            documents.extend(page)
            if not cursor:
                return documents

    def map(self, url: str, **options: Any) -> list[dict[str, Any]]:
        """Discover URLs without fetching page bodies. Fast and cheap."""
        return self._post("/v1/map", {"url": url, **options}).get("links", [])

    def products(self, url: str, **options: Any) -> dict[str, Any]:
        """Every product a Shopify or WooCommerce store publishes, from its own
        catalogue endpoint. Returns {platform, total, products, pages_fetched, cost}."""
        return self._post("/v1/products", {"url": url, **options})

    def posts(self, url: str, **options: Any) -> dict[str, Any]:
        """A site's posts from its API (WordPress, Substack, Squarespace,
        Discourse) or its RSS/Atom feed. Returns {platform, source, posts, cost}."""
        return self._post("/v1/posts", {"url": url, **options})

    def company(self, url: str, **options: Any) -> dict[str, Any]:
        """Everything a company's own site says about itself: firmographics
        (name, phone, address, LinkedIn, headcount, industry) plus, unless
        `contacts=False`, the emails, social links and contact form it
        publishes. Returns {domain, company, people, contacts, pagesRead, cost}."""
        return self._post("/v1/company", {"url": url, **options})

    def domain(self, domain: str, **options: Any) -> dict[str, Any]:
        """What is known about a domain rather than a page: registration,
        DNS records, and who links to it in our own crawl graph. Every part
        is opt-out — pass `registration=False`, `dns=False` or
        `backlinks=False` to skip one. Returns {domain, registration, dns,
        backlinks, cost}."""
        return self._post("/v1/domain", {"domain": domain, **options})

    # --- monitors: watch pages for changes on a schedule ------------------

    def create_monitor(self, name: str, urls: list[str] | str, **options: Any) -> dict[str, Any]:
        """intervalMinutes (>= 5, default 60), goal, webhook. Returns the monitor."""
        body: dict[str, Any] = {"name": name, **options}
        body["urls" if isinstance(urls, list) else "url"] = urls
        return self._post("/v1/monitor", body)

    def monitors(self) -> list[dict[str, Any]]:
        return self._get("/v1/monitor").get("monitors", [])

    def monitor(self, monitor_id: str) -> dict[str, Any]:
        return self._get(f"/v1/monitor/{monitor_id}")

    def delete_monitor(self, monitor_id: str) -> None:
        self._delete(f"/v1/monitor/{monitor_id}")

    def run_monitor(self, monitor_id: str) -> dict[str, Any]:
        """A check right now; returns it."""
        return self._post(f"/v1/monitor/{monitor_id}/run", {})

    def monitor_checks(self, monitor_id: str, limit: int = 20) -> list[dict[str, Any]]:
        out = self._get(f"/v1/monitor/{monitor_id}/checks", params={"limit": limit})
        return out.get("checks", [])

    def batch_scrape(self, urls: list[str], **options: Any) -> CrawlJob:
        data = self._post("/v1/batch/scrape", {"urls": urls, **options})
        return CrawlJob(id=data["id"], status=data["status"])

    def extract(
        self,
        urls: list[str],
        schema: dict[str, Any] | None = None,
        prompt: str | None = None,
        *,
        template: str | None = None,
        **options: Any,
    ) -> list[dict[str, Any]]:
        """Schema-constrained extraction.

        Give either your own `schema` or a named `template` — `product`,
        `article`, `jobPosting` and the rest, which `templates()` lists. A
        template is a curated schema whose fields are the ones sites already
        publish in their own markup, so they usually come back without a model
        being called at all.

        Output is validated before it is returned, so a row with an `error`
        means the page genuinely lacked the fields — not that the request
        should be retried.
        """
        if (schema is None) == (template is None):
            raise ValueError(
                "give either `schema` or `template`, not both and not neither: "
                "a template IS a schema, and silently picking one would return "
                "fields you did not ask for"
            )
        body: dict[str, Any] = {"urls": urls, **options}
        if schema is not None:
            body["schema"] = schema
        if template is not None:
            body["template"] = template
        if prompt:
            body["prompt"] = prompt
        return self._post("/v1/extract", body)

    def templates(self) -> list[dict[str, Any]]:
        """The named templates this deployment offers, with their fields."""
        answer = self._get("/v1/templates")
        return list(answer.get("templates", []))

    def search(self, query: str, **options: Any) -> dict[str, Any]:
        return self._post("/v1/search", {"query": query, **options})

    def parse(
        self,
        *,
        url: str | None = None,
        path: str | os.PathLike[str] | None = None,
        content: str | bytes | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Turn a document into markdown: PDF, DOCX, HTML, TXT or Markdown.

        Exactly one of:
          * `path`: a file on disk, uploaded as it is (binary-safe, so a PDF
            survives the trip).
          * `content`: a document already in memory; `filename` says what
            kind it is (default `document.txt` for text, `document.pdf` is
            yours to name for bytes).
          * `url`: a document on the web. The engine fetches it with every
            tier it has and reads PDFs, HTML and text, then returns the same
            shape as an upload.

        `/v1/parse` takes a multipart file and nothing else. This used to post
        JSON to it, so every call failed, URL or file alike, and a local PDF
        was read as UTF-8 text on the way (found 23 Sep 2026).
        """
        kind = _parse_input(url, path, content)
        if kind == "url":
            assert url is not None
            return _parse_from_scrape(url, self._post("/v1/scrape", _parse_scrape_body(url)))
        name, data = _parse_upload(path, content, filename)
        return self._unwrap(
            self._client.post(
                f"{self.base_url}/v1/parse",
                files={"file": (name, data)},
                timeout=self._default_timeout,
            )
        )

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SnoopScan:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class AsyncSnoopScan:
    """Async client. Same surface as the synchronous one."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._default_timeout = timeout
        self._client = httpx.AsyncClient(
            timeout=timeout,
            # No client-wide Content-Type: httpx sets JSON for a JSON body and
            # the multipart boundary for an upload. A fixed JSON header here
            # overrode the boundary, so /v1/parse never saw its file.
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        request_timeout = _http_timeout_for(body, self._default_timeout)
        url = f"{self.base_url}{path}"
        response = await self._client.post(url, json=body, timeout=request_timeout)
        return SnoopScan._unwrap(response)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._client.get(f"{self.base_url}{path}", params=params)
        return SnoopScan._unwrap(response)

    async def _delete(self, path: str) -> Any:
        return SnoopScan._unwrap(await self._client.delete(f"{self.base_url}{path}"))

    async def scrape(self, url: str, **options: Any) -> Document:
        return Document.from_payload(await self._post("/v1/scrape", {"url": url, **options}))

    async def crawl(self, url: str, **options: Any) -> CrawlJob:
        data = await self._post("/v1/crawl", {"url": url, **options})
        return CrawlJob(id=data["id"], status=data["status"])

    async def crawl_status(self, job_id: str) -> CrawlJob:
        data = await self._get(f"/v1/crawl/{job_id}")
        return CrawlJob(
            id=data["id"],
            status=data["status"],
            total=data.get("total", 0),
            completed=data.get("completed", 0),
            failed=data.get("failed", 0),
            cost=data.get("cost") or {},
        )

    async def crawl_pages(
        self, job_id: str, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[Document], str | None]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = await self._get(f"/v1/crawl/{job_id}/pages", params)
        documents = [Document.from_payload(row) for row in data.get("pages", [])]
        next_link = data.get("next")
        next_cursor = next_link.split("cursor=")[-1] if next_link else None
        return documents, next_cursor

    async def crawl_errors(self, job_id: str) -> list[dict[str, Any]]:
        return (await self._get(f"/v1/crawl/{job_id}/errors")).get("errors", [])

    async def cancel_crawl(self, job_id: str) -> CrawlJob:
        payload = SnoopScan._unwrap(await self._client.delete(f"{self.base_url}/v1/crawl/{job_id}"))
        return CrawlJob(id=payload["id"], status=payload["status"])

    async def crawl_and_wait(
        self,
        url: str,
        poll_interval: float = 3.0,
        max_wait: float = 900.0,
        **options: Any,
    ) -> list[Document]:
        """Start a crawl and wait until it finishes, then return every page.

        Convenience only. For anything large, start the crawl and read pages
        as they arrive rather than holding the whole result in memory.
        """
        job = await self.crawl(url, **options)
        deadline = time.monotonic() + max_wait

        while time.monotonic() < deadline:
            current = await self.crawl_status(job.id)
            if current.finished:
                break
            await asyncio.sleep(poll_interval)
        else:
            raise SnoopScanError("TIMEOUT", f"Crawl {job.id} did not finish within {max_wait:.0f}s")

        documents: list[Document] = []
        cursor: str | None = None
        while True:
            page, cursor = await self.crawl_pages(job.id, cursor)
            documents.extend(page)
            if not cursor:
                return documents

    async def map(self, url: str, **options: Any) -> list[dict[str, Any]]:
        return (await self._post("/v1/map", {"url": url, **options})).get("links", [])

    async def products(self, url: str, **options: Any) -> dict[str, Any]:
        return await self._post("/v1/products", {"url": url, **options})

    async def posts(self, url: str, **options: Any) -> dict[str, Any]:
        return await self._post("/v1/posts", {"url": url, **options})

    async def company(self, url: str, **options: Any) -> dict[str, Any]:
        return await self._post("/v1/company", {"url": url, **options})

    async def domain(self, domain: str, **options: Any) -> dict[str, Any]:
        return await self._post("/v1/domain", {"domain": domain, **options})

    async def batch_scrape(self, urls: list[str], **options: Any) -> CrawlJob:
        data = await self._post("/v1/batch/scrape", {"urls": urls, **options})
        return CrawlJob(id=data["id"], status=data["status"])

    async def search(self, query: str, **options: Any) -> dict[str, Any]:
        return await self._post("/v1/search", {"query": query, **options})

    async def parse(
        self,
        *,
        url: str | None = None,
        path: str | os.PathLike[str] | None = None,
        content: str | bytes | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """As `SnoopScan.parse`."""
        kind = _parse_input(url, path, content)
        if kind == "url":
            assert url is not None
            return _parse_from_scrape(url, await self._post("/v1/scrape", _parse_scrape_body(url)))
        name, data = _parse_upload(path, content, filename)
        response = await self._client.post(
            f"{self.base_url}/v1/parse",
            files={"file": (name, data)},
            timeout=self._default_timeout,
        )
        return SnoopScan._unwrap(response)

    async def create_monitor(
        self, name: str, urls: list[str] | str, **options: Any
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, **options}
        body["urls" if isinstance(urls, list) else "url"] = urls
        return await self._post("/v1/monitor", body)

    async def monitors(self) -> list[dict[str, Any]]:
        return (await self._get("/v1/monitor")).get("monitors", [])

    async def monitor(self, monitor_id: str) -> dict[str, Any]:
        return await self._get(f"/v1/monitor/{monitor_id}")

    async def delete_monitor(self, monitor_id: str) -> None:
        await self._delete(f"/v1/monitor/{monitor_id}")

    async def run_monitor(self, monitor_id: str) -> dict[str, Any]:
        return await self._post(f"/v1/monitor/{monitor_id}/run", {})

    async def monitor_checks(self, monitor_id: str, limit: int = 20) -> list[dict[str, Any]]:
        out = await self._get(f"/v1/monitor/{monitor_id}/checks", params={"limit": limit})
        return out.get("checks", [])

    async def extract(
        self,
        urls: list[str],
        schema: dict[str, Any] | None = None,
        prompt: str | None = None,
        *,
        template: str | None = None,
        **options: Any,
    ) -> list[dict[str, Any]]:
        """Your own `schema` or a named `template`. See SnoopScan.extract."""
        if (schema is None) == (template is None):
            raise ValueError(
                "give either `schema` or `template`, not both and not neither: "
                "a template IS a schema, and silently picking one would return "
                "fields you did not ask for"
            )
        body: dict[str, Any] = {"urls": urls, **options}
        if schema is not None:
            body["schema"] = schema
        if template is not None:
            body["template"] = template
        if prompt:
            body["prompt"] = prompt
        return await self._post("/v1/extract", body)

    async def templates(self) -> list[dict[str, Any]]:
        """The named templates this deployment offers, with their fields."""
        answer = await self._get("/v1/templates")
        return list(answer.get("templates", []))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncSnoopScan:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


# -- parse ----------------------------------------------------------------
# Shared by both clients, so the two cannot disagree about what parse sends.

_DOCUMENT_ONLY_BY_UPLOAD = (".docx",)


def _parse_input(url: Any, path: Any, content: Any) -> str:
    given = [
        name
        for name, value in (("url", url), ("path", path), ("content", content))
        if value is not None and value != ""
    ]
    if len(given) != 1:
        raise ValueError("parse takes exactly one of url, path or content")
    if given[0] == "url" and str(url).lower().split("?", 1)[0].endswith(_DOCUMENT_ONLY_BY_UPLOAD):
        raise ValueError(
            "A Word document is read from a file, not a URL: download it and pass path=."
        )
    return given[0]


def _parse_upload(path: Any, content: Any, filename: str | None) -> tuple[str, bytes]:
    if path is not None:
        with open(path, "rb") as handle:
            return filename or os.path.basename(os.fspath(path)), handle.read()
    if isinstance(content, str):
        return filename or "document.txt", content.encode("utf-8")
    return filename or "document", bytes(content)


def _parse_scrape_body(url: str) -> dict[str, Any]:
    return {"url": url, "formats": ["markdown"], "parsers": ["pdf"]}


def _parse_from_scrape(url: str, data: dict[str, Any]) -> dict[str, Any]:
    """A scrape answer in the shape `/v1/parse` returns, so callers need not care which ran."""
    meta = data.get("metadata") or {}
    return {
        "markdown": data.get("markdown") or "",
        "url": meta.get("sourceURL") or meta.get("url") or url,
        "kind": "url",
        "metadata": meta,
        "cost": data.get("cost"),
    }
