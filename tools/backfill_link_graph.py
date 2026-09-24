"""Fill `domain_links` from the links already stored on every page.

Not done in migration 0027 on purpose: what counts as a "domain" is decided by
`urls.registrable_domain`, which consults the public suffix list, and a SQL
regex approximating it would be a second definition of the same thing. This
uses the real function.

Safe to re-run. The upsert takes the LARGER link count for a pair rather than
adding, so a second pass over the same pages changes nothing.

    .venv/bin/python tools/backfill_link_graph.py
"""

from __future__ import annotations

import asyncio
import sys
import time

BATCH = 500


async def main() -> int:
    from engine.storage import db
    from engine.storage import repositories as repo

    pool = await db.get_pool()
    started = time.monotonic()
    pages = written = 0

    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT count(*) FROM pages WHERE links IS NOT NULL")
        print(f"{total} pages with links", flush=True)

        last_id = ""
        while True:
            rows = await conn.fetch(
                "SELECT id, url, links FROM pages "
                "WHERE links IS NOT NULL AND id > $1 ORDER BY id LIMIT $2",
                last_id,
                BATCH,
            )
            if not rows:
                break
            for row in rows:
                last_id = row["id"]
                links = row["links"]
                if isinstance(links, str):
                    import json

                    links = json.loads(links)
                written += await repo.record_links(row["url"], list(links or []))
                pages += 1
            print(
                f"  {pages}/{total} pages · {written} pairs · {time.monotonic() - started:.0f}s",
                end="\r",
                flush=True,
            )

        print()
        pairs = await conn.fetchval("SELECT count(*) FROM domain_links")
        targets = await conn.fetchval("SELECT count(DISTINCT target_domain) FROM domain_links")
        print(f"done: {pairs} pairs, {targets} target domains, {time.monotonic() - started:.0f}s")

    await db.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
