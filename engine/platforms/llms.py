"""llms.txt — a site's own curated summary for machines (llmstxt.org). Present on
a third of the sites measured; one request, and exactly what an agent wants."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import structlog

logger = structlog.get_logger(__name__)


async def llms_txt(url: str, fetcher: Any) -> dict[str, str] | None:
    from engine.core.fetch.base import FetchRequest

    p = urlsplit(url)
    root = f"{p.scheme}://{p.netloc}"
    out: dict[str, str] = {}
    for name in ("llms.txt", "llms-full.txt"):
        try:
            res = await fetcher.fetch(FetchRequest(url=f"{root}/{name}", timeout_ms=10_000))
        except Exception as exc:  # noqa: BLE001 - most sites do not publish one
            logger.debug("llms_txt_unavailable", url=f"{root}/{name}", error=str(exc))
            continue
        if res.status_code != 200 or not res.body:
            continue
        body = res.text(limit=2_000_000)
        if "<html" in body[:600].lower():
            continue  # a soft-404 page, not the file
        out[name] = body
    return out or None
