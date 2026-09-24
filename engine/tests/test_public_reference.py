"""The published reference is generated, so the model IS the documentation.

A request field with no description reaches the docs site as a blank row. The
fix is not to write it twice — it is to make the omission fail here, where the
field is added.
"""

from __future__ import annotations

import pytest

from engine.core.models import ScrapeRequest


def _public_properties(model: type) -> dict:
    schema = model.model_json_schema()
    return {
        name: spec
        for name, spec in (schema.get("properties") or {}).items()
        if not spec.get("x-internal")
    }


@pytest.mark.parametrize("model", [ScrapeRequest])
def test_every_public_request_field_documents_itself(model: type) -> None:
    undocumented = [
        name for name, spec in _public_properties(model).items() if not spec.get("description")
    ]
    assert not undocumented, (
        f"{model.__name__} fields reach the public reference with no description: {undocumented}. "
        "Add Field(description=...) on the model, or mark it json_schema_extra=INTERNAL."
    )


def test_the_fetch_ladder_is_never_published() -> None:
    """Tier controls stay in the contract and out of the reference.

    Naming their values publishes the order we try things in, which is exactly
    what the anti-bot vendors whose challenges we clear would like to read.
    """
    schema = ScrapeRequest.model_json_schema()
    props = schema.get("properties") or {}

    for name in ("tier", "escalate", "maxTier"):
        assert props[name].get("x-internal") is True, f"{name} would be published"

    # And no PUBLIC field may name a rung in its description.
    rungs = ("impersonate", "stealth", "stealth_hard", "camoufox", "patchright")
    for name, spec in _public_properties(ScrapeRequest).items():
        text = (spec.get("description") or "").lower()
        leaked = [r for r in rungs if r in text]
        assert not leaked, f"{name} names the ladder in public copy: {leaked}"


def test_robots_is_opt_in_not_opt_out() -> None:
    """The operator decides, not the target.

    Pinned because it is a one-word change that would revert silently: the
    whole suite passed with it either way, so nothing else was watching.
    """
    from engine.core.models import CrawlRequest, ScrapeRequest

    # Read off the MODEL, not the public schema: the field is deliberately
    # unlisted, so the published reference is no longer the place to assert it.
    assert ScrapeRequest.model_fields["respectRobots"].default is False
    assert CrawlRequest.model_fields["respectRobots"].default is False

    # And it must stay unlisted: the default posture is not a thing we publish.
    assert (
        ScrapeRequest.model_json_schema()["properties"]["respectRobots"].get("x-internal") is True
    )


def test_the_published_schema_leaks_no_internals() -> None:
    """Hiding a field is not hiding the type it points at.

    `tier` was marked internal and duly vanished from the reference — while the
    Tier enum sat in components/schemas listing every rung of the ladder to
    anyone who fetched /openapi.json.
    """
    import json

    from engine.api.app import app

    schema = app.openapi()
    blob = json.dumps(schema).lower()

    for rung in ("impersonate", "stealth_hard", "camoufox", "patchright"):
        assert rung not in blob, f"the published schema names {rung}"

    for internal in ("owner_ref", "politeness", "crawl_delay", "rate_limit_rpm"):
        assert internal not in blob, f"the published schema names {internal}"

    # The control plane is not customer surface.
    assert not [p for p in schema.get("paths", {}) if "internal" in p]


def test_stripping_internals_does_not_gut_the_schema() -> None:
    """A sweep that removes too much is a broken reference, not a safe one."""
    from engine.api.app import app

    schema = app.openapi()
    components = (schema.get("components") or {}).get("schemas") or {}
    props = components["ScrapeRequest"]["properties"]

    assert "url" in props and "formats" in props and "onlyMainContent" in props
    assert "tier" not in props and "respectRobots" not in props
    assert len(schema["paths"]) >= 15, "endpoints went missing"

    # Every $ref still resolves: pruning must not orphan a live reference.
    import json
    import re

    for ref in set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(schema))):
        assert ref in components, f"dangling $ref after pruning: {ref}"
