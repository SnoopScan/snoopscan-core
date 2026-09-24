"""Python client for the SnoopScan web scraping API."""

from snoopscan.client import (
    AsyncSnoopScan,
    Cost,
    CrawlJob,
    Document,
    SnoopScan,
    SnoopScanError,
)

__all__ = [
    "AsyncSnoopScan",
    "Cost",
    "CrawlJob",
    "Document",
    "SnoopScan",
    "SnoopScanError",
]


def _version() -> str:
    """From the installed distribution, so pyproject.toml is the only place the
    number is written. A client that reports a version it is not is worse than
    one that says it does not know."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("snoopscan")
    except PackageNotFoundError:
        return "0.0.0+unknown"


__version__ = _version()
