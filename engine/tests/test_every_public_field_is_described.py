"""Every public request field carries a description.

The site's parameter tables are generated from this schema, so a field with no
description is a blank dash on the public docs. 58 of 84 were blank until
21 Sep 2026 — every field of Crawl, Map, Search, Batch, Monitor, Posts,
Products and SERP — and nothing noticed, because nothing looked.
"""

from __future__ import annotations

from typing import Any

from engine.api.app import app


def test_every_public_request_field_has_a_description() -> None:
    doc: dict[str, Any] = app.openapi()
    schemas = doc["components"]["schemas"]

    def resolve(schema: dict[str, Any]) -> dict[str, Any]:
        ref = schema.get("$ref", "")
        return schemas.get(ref.split("/")[-1], {}) if ref else schema

    blank = []
    for path, ops in doc["paths"].items():
        op = ops.get("post")
        if not op or "/internal" in path:
            continue
        body = op.get("requestBody", {}).get("content", {}).get("application/json", {})
        for name, prop in resolve(body.get("schema", {})).get("properties", {}).items():
            if not (prop.get("description") or "").strip():
                blank.append(f"{path} {name}")
    assert not blank, "shows as a blank dash on the public docs:\n  " + "\n  ".join(blank)
