"""MCP guardrails (08-mcp-server.md section 5).

An agent with a scraping tool can spend real money quickly and can behave badly
toward third-party sites. These limits are non-negotiable.

Refusals are explicit and explain the limit. An agent given a bare error code
retries the identical request; one told what the cap is adapts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Hard caps per MCP session.
MAX_PAGES_PER_SESSION = 500
MAX_CONCURRENT_CRAWLS = 2
MAX_CRAWL_LIMIT = 500
MAX_FETCH_CONTENT_RESULTS = 20
MAX_EXTRACT_URLS = 10
MAX_SEARCH_LIMIT = 20

# Default proxy bandwidth ceiling per session, in bytes. Configurable, but a
# session must never be able to spend unboundedly.
DEFAULT_BANDWIDTH_CAP_BYTES = 500 * 1024 * 1024


class GuardrailExceeded(Exception):
    """Raised with a message written for a model to act on."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class SessionBudget:
    """Per-session accounting. One instance per MCP connection."""

    pages_fetched: int = 0
    proxy_bytes: int = 0
    active_crawls: set[str] = field(default_factory=set)
    bandwidth_cap_bytes: int = DEFAULT_BANDWIDTH_CAP_BYTES

    # -- pages ------------------------------------------------------------

    def check_pages(self, requested: int = 1) -> None:
        if self.pages_fetched + requested > MAX_PAGES_PER_SESSION:
            remaining = max(0, MAX_PAGES_PER_SESSION - self.pages_fetched)
            raise GuardrailExceeded(
                f"This session has fetched {self.pages_fetched} pages and the limit is "
                f"{MAX_PAGES_PER_SESSION}. {remaining} remain. Narrow what you are "
                f"fetching, or start the work through the REST API where a human sets "
                f"the budget."
            )

    def record_pages(self, count: int = 1) -> None:
        self.pages_fetched += count

    # -- bandwidth --------------------------------------------------------

    def check_bandwidth(self) -> None:
        if self.proxy_bytes >= self.bandwidth_cap_bytes:
            raise GuardrailExceeded(
                f"This session has used its proxy bandwidth allowance "
                f"({self.proxy_bytes / 1_048_576:.0f}MB of "
                f"{self.bandwidth_cap_bytes / 1_048_576:.0f}MB). No further proxied "
                f"requests will run. Continue with cached content, or ask a human to "
                f"raise the cap."
            )

    def record_bandwidth(self, bytes_used: int) -> None:
        self.proxy_bytes += bytes_used

    # -- crawls -----------------------------------------------------------

    def check_crawl_slot(self) -> None:
        if len(self.active_crawls) >= MAX_CONCURRENT_CRAWLS:
            raise GuardrailExceeded(
                f"{len(self.active_crawls)} crawls are already running in this session "
                f"and the limit is {MAX_CONCURRENT_CRAWLS}. Wait for one to finish "
                f"(check with crawlStatus) before starting another."
            )

    def start_crawl(self, job_id: str) -> None:
        self.active_crawls.add(job_id)

    def finish_crawl(self, job_id: str) -> None:
        self.active_crawls.discard(job_id)


def clamp_crawl_limit(requested: int) -> int:
    """MCP crawl defaults are far lower than the REST API's on purpose.

    An agent should not be able to start a 10,000-page crawl from a casual
    instruction. A larger crawl goes through REST, where a human set it up.
    """
    return min(max(1, requested), MAX_CRAWL_LIMIT)


def check_extract_urls(count: int) -> None:
    if count > MAX_EXTRACT_URLS:
        raise GuardrailExceeded(
            f"extract accepts at most {MAX_EXTRACT_URLS} URLs per call and {count} were "
            f"given. Split them across several calls, or narrow the list to the pages "
            f"most likely to carry the fields you need."
        )


def check_search_limit(limit: int, fetch_content: bool) -> int:
    """`fetchContent: true` with a high limit is a fan-out of full page fetches.

    Models will do this unless the tool stops them.
    """
    if fetch_content and limit > MAX_FETCH_CONTENT_RESULTS:
        raise GuardrailExceeded(
            f"fetchContent is limited to {MAX_FETCH_CONTENT_RESULTS} results and "
            f"{limit} were requested — each one is a full page fetch. Either lower "
            f"the limit, or set fetchContent to false and fetch only the results that "
            f"look worth reading."
        )
    return min(limit, MAX_SEARCH_LIMIT)
