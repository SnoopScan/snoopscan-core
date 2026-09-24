"""Named schemas for the pages people actually scrape.

A caller who wants a product's price should not have to write a JSON schema,
and should not pay a model to read a price that is already in the page's own
markup. A template is a curated schema with a name: ask for `product` and the
same fields come back from every shop that publishes ordinary structured data.

Field names follow schema.org deliberately. That is what shops, papers and job
boards already publish in JSON-LD, so `from_markup` fills these for free and
the model is never called — which is the difference between a template that
costs nothing and one that bills tokens per page.

Deliberately NOT per-site recipes. A library of "Amazon", "eBay", "Zillow"
extractors is a maintenance debt that rots on the next redesign, and it only
ever covers the sites someone got round to writing. A page-type template works
on every site that publishes the markup, which is most of commerce, news and
hiring.

WHERE THEY LIVE, and how they change without a deploy:

  templates.yaml   the floor that ships with the engine — a data file, not
                   code, for the same reason site_rules.yaml is one.
  the database     `extraction_templates`, desk-managed: add a template, or
                   override a shipped one when a field list stops matching
                   what sites publish. `refresh()` reloads it; `invalidate()`
                   is called by the writes so an edit is live within a request.

`effective()` is the merge, and it is what /v1/templates serves and what a
`template:` on a request resolves against. A shipped template that nobody has
overridden is served straight from the file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger(__name__)

_PATH = Path(__file__).with_name("templates.yaml")

# Desk-managed templates, loaded from the database. Empty until refresh() runs,
# which the API does at startup and after every write — so a deployment with no
# database (the open core, a test) simply serves the shipped file.
_OVERRIDES: dict[str, dict[str, Any]] = {}


@lru_cache(maxsize=1)
def _shipped() -> dict[str, dict[str, Any]]:
    """What the engine ships with. Cached: the file does not change under us."""
    try:
        doc = yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:  # pragma: no cover - a broken file is fatal
        logger.error("templates_file_unreadable", path=str(_PATH), error=str(exc)[:200])
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in doc.get("templates") or []:
        name = str(entry.get("name") or "").strip()
        schema = entry.get("schema")
        if name and isinstance(schema, dict):
            out[name] = {
                "name": name,
                "description": str(entry.get("description") or ""),
                "schema": schema,
                "source": "shipped",
            }
    return out


async def refresh() -> int:
    """Reload the desk's templates. Returns how many are in force."""
    global _OVERRIDES
    try:
        from engine.storage import repositories as repo

        rows = await repo.list_extraction_templates()
    except ImportError:  # no storage layer in this deployment
        return 0
    except Exception as exc:  # noqa: BLE001 - a registry outage must not break extraction
        logger.warning("templates_load_failed", error=str(exc)[:200])
        return len(_OVERRIDES)
    _OVERRIDES = {
        r["name"]: {
            "name": r["name"],
            "description": r.get("description") or "",
            "schema": r["schema"],
            "source": "desk",
        }
        for r in rows
        if r.get("active", True)
    }
    return len(_OVERRIDES)


def invalidate() -> None:
    """Drop the desk's copy so the next refresh re-reads it."""
    _OVERRIDES.clear()


def effective() -> dict[str, dict[str, Any]]:
    """Shipped templates, with the desk's additions and overrides on top."""
    merged = dict(_shipped())
    merged.update(_OVERRIDES)
    return merged


def schema_for(name: str) -> dict[str, Any] | None:
    """The schema behind a template name, or None if there is no such template."""
    entry = effective().get(name)
    return entry["schema"] if entry else None


def names() -> tuple[str, ...]:
    return tuple(effective())


def catalogue() -> list[dict[str, Any]]:
    """Every template with its fields, for listing to a caller."""
    return [
        {
            "name": entry["name"],
            "description": entry["description"],
            "fields": sorted(entry["schema"].get("properties") or {}),
            "required": entry["schema"].get("required", []),
            "source": entry["source"],
        }
        for entry in effective().values()
    ]


# Kept for the modules that read the shipped set directly (tests, the drift
# check). The live answer is always `effective()`.
def shipped() -> dict[str, dict[str, Any]]:
    return dict(_shipped())
