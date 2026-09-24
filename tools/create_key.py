#!/usr/bin/env python3
"""Create an API key. The plaintext is printed once and never stored.

    python tools/create_key.py "internal-pipeline" --rpm 600

Only the SHA-256 hash goes to the database, so a lost key is reissued rather
than recovered.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets

from engine.core.credits import OPERATOR_OWNER
from engine.storage import db
from engine.storage.repositories import create_api_key


async def main() -> int:
    parser = argparse.ArgumentParser(description="Create an API key")
    parser.add_argument("label", help="Human-readable label for this key")
    parser.add_argument(
        "--owner",
        default=OPERATOR_OWNER,
        help=(
            "Owner this key's usage is billed to. Defaults to the operator "
            "owner, which is metered but never refused for want of credits. "
            "A key with no owner is not possible: it would fetch unmetered."
        ),
    )
    parser.add_argument("--rpm", type=int, default=60, help="Rate limit, requests per minute")
    parser.add_argument(
        "--scopes",
        default="scrape,crawl,map",
        help="Comma-separated scopes",
    )
    parser.add_argument(
        "--allow-js-exec",
        action="store_true",
        help="Permit the executeJavascript action (off by default; never on MCP)",
    )
    args = parser.parse_args()

    plaintext = f"sk_{secrets.token_urlsafe(32)}"
    webhook_secret = secrets.token_urlsafe(32)

    try:
        key_id = await create_api_key(
            plaintext,
            args.label,
            owner_ref=args.owner,
            scopes=[s.strip() for s in args.scopes.split(",") if s.strip()],
            rate_limit_rpm=args.rpm,
            allow_js_exec=args.allow_js_exec,
            webhook_secret=webhook_secret,
        )
    finally:
        await db.close_pool()

    print(f"id:             {key_id}")
    print(f"label:          {args.label}")
    print(f"key:            {plaintext}")
    print(f"webhook secret: {webhook_secret}")
    print("\nStore these now — they are not recoverable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
