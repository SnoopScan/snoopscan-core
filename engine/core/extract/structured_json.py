"""Schema-constrained extraction (04-extraction.md section 4c).

Order matters and is a cost control:

  1. extract candidate content via the normal paths
  2. pull structured markup (JSON-LD, microdata, Open Graph) — free, and often
     answers the schema outright
  3. only if the schema is still unsatisfied, call a model
  4. VALIDATE the output against the schema before returning
  5. on validation failure, retry once with the errors fed back, then fail

Step 4 is the whole point. A price field returning "about £40" instead of 40.0
has failed, and returning it as success poisons whatever consumes it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_NUMBER = re.compile(r"-?\d[\d,]*\.?\d*")
_TRUE_WORDS = {"true", "yes", "in stock", "available", "instock"}
_FALSE_WORDS = {"false", "no", "out of stock", "unavailable", "outofstock", "sold out"}


@dataclass
class ExtractionOutcome:
    data: dict[str, Any] | None = None
    confidence: float = 0.0
    error: str | None = None
    source: str = "markup"  # markup | model
    validation_errors: list[str] = field(default_factory=list)


class SchemaError(Exception):
    pass


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate(data: Any, schema: dict[str, Any], path: str = "") -> list[str]:
    """Validate against a JSON Schema subset: types, required, nesting, enums.

    Deliberately not a full JSON Schema implementation — this covers what the
    API contract actually accepts, and an unrecognised keyword is ignored
    rather than silently treated as satisfied.
    """
    errors: list[str] = []
    expected = schema.get("type")
    where = path or "(root)"

    if expected == "object":
        if not isinstance(data, dict):
            return [f"{where}: expected an object, got {_name(data)}"]
        for name in schema.get("required", []):
            if name not in data or data[name] is None:
                errors.append(f"{where}.{name}: required field is missing")
        for name, subschema in (schema.get("properties") or {}).items():
            if name in data and data[name] is not None:
                errors.extend(validate(data[name], subschema, f"{where}.{name}"))
        return errors

    if expected == "array":
        if not isinstance(data, list):
            return [f"{where}: expected an array, got {_name(data)}"]
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(data):
                errors.extend(validate(item, item_schema, f"{where}[{index}]"))
        return errors

    if data is None:
        return []

    if expected == "string" and not isinstance(data, str):
        errors.append(f"{where}: expected a string, got {_name(data)}")
    elif expected == "number" and not (
        isinstance(data, int | float) and not isinstance(data, bool)
    ):
        errors.append(f"{where}: expected a number, got {_name(data)}")
    elif expected == "integer" and (not isinstance(data, int) or isinstance(data, bool)):
        errors.append(f"{where}: expected an integer, got {_name(data)}")
    elif expected == "boolean" and not isinstance(data, bool):
        errors.append(f"{where}: expected a boolean, got {_name(data)}")

    allowed = schema.get("enum")
    if allowed is not None and data not in allowed:
        errors.append(f"{where}: {data!r} is not one of {allowed}")

    return errors


def _name(value: Any) -> str:
    if value is None:
        return "null"
    return {
        str: "a string",
        bool: "a boolean",
        int: "an integer",
        float: "a number",
        list: "an array",
        dict: "an object",
    }.get(type(value), type(value).__name__)


# --------------------------------------------------------------------------
# Coercion — only where the intent is unambiguous
# --------------------------------------------------------------------------


def coerce(value: Any, schema: dict[str, Any]) -> Any:
    """Convert an obviously-right value into the declared type.

    Narrow on purpose. "£129.99" -> 129.99 is unambiguous; "about £40" is not,
    and must fail rather than be guessed at.
    """
    expected = schema.get("type")
    if value is None or expected is None:
        return value

    if expected in ("number", "integer") and isinstance(value, str):
        stripped = value.strip()
        # A bare currency-prefixed number is safe. Anything with prose is not.
        if re.fullmatch(r"[^\d\-]{0,3}\s*-?\d[\d,]*\.?\d*\s*[A-Za-z]{0,3}", stripped):
            match = _NUMBER.search(stripped)
            if match:
                numeric = match.group(0).replace(",", "")
                try:
                    return int(numeric) if expected == "integer" else float(numeric)
                except ValueError:
                    return value
        return value

    if expected == "boolean" and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        return value

    if expected == "string" and isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)

    if expected == "object" and isinstance(value, dict):
        properties = schema.get("properties") or {}
        return {k: coerce(v, properties.get(k, {})) for k, v in value.items()}

    if expected == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            return [coerce(item, item_schema) for item in value]
    return value


# --------------------------------------------------------------------------
# Free pass: answer the schema from structured markup
# --------------------------------------------------------------------------

# Schema field name -> the JSON-LD / Open Graph keys that usually hold it.
_MARKUP_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "headline", "title", "og:title"),
    "title": ("headline", "name", "title", "og:title"),
    "description": ("description", "og:description"),
    "price": ("price", "lowPrice", "highPrice"),
    "currency": ("priceCurrency", "currency"),
    "sku": ("sku", "mpn", "gtin"),
    "brand": ("brand",),
    "author": ("author",),
    "email": ("email",),
    "telephone": ("telephone", "phone"),
    "url": ("url", "og:url"),
    "image": ("image", "og:image"),
    "datePublished": ("datePublished", "dateCreated"),
    "inStock": ("availability",),
    "availability": ("availability",),
}


def from_markup(structured_hints: dict[str, Any] | None, schema: dict[str, Any]) -> dict[str, Any]:
    """Fill what structured markup already answers. Costs nothing."""
    if not structured_hints:
        return {}

    flat = _flatten(structured_hints)
    out: dict[str, Any] = {}
    for name, subschema in (schema.get("properties") or {}).items():
        for alias in _MARKUP_ALIASES.get(name, (name,)):
            if alias in flat and flat[alias] is not None:
                value = flat[alias]
                if name in ("inStock",) and isinstance(value, str):
                    value = "instock" in value.lower().replace("/", "").replace(" ", "")
                out[name] = coerce(value, subschema)
                break
    return out


def _list_of_text(values: list[Any]) -> list[str]:
    """A markup list read as the list of strings a reader would see.

    Sites write the same list three ways: plain strings, entities carrying a
    `name` (a list of authors), and step objects carrying `text` (HowToStep).
    A list holding none of those — `offers`, `review` — returns empty, and the
    caller descends into it instead.
    """
    out: list[str] = []
    for item in values:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            for field in ("name", "text", "headline"):
                text = item.get(field)
                if isinstance(text, str) and text.strip():
                    out.append(text.strip())
                    break
    return out


def _flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    """One level of key lookup across nested markup (offers.price -> price).

    Two rules, both learned from real pages:

    A PARENT'S OWN KEY WINS over a child's of the same name. A product carries
    `name` and a `brand` object that also carries `name`; the nested one used
    to overwrite the product's, so every branded product came back named after
    its brand.

    AN ENTITY STANDS FOR ITS NAME under the parent's key. Sites publish
    `author`, `publisher` and `brand` as objects — {"@type": "Person", "name":
    "..."} — and flattening threw the object away, so `author` was simply
    absent and a model was called to read a name the page had already given us.

    A LIST OF THINGS STAYS A LIST, under its own key. Lists used only to be
    descended into, so `recipeIngredient: ["2 onions", ...]` vanished and the
    steps inside `recipeInstructions` surfaced as a bare top-level `text` — the
    ingredients and method of every recipe on the web, dropped, and a stray key
    left where a schema might match it by accident (20 Sep 2026). Descending
    still happens afterwards, so `offers: [{price: ...}]` lifts as before.
    """
    out: dict[str, Any] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                named = value.get("name")
                if isinstance(named, str) and named.strip():
                    out.setdefault(key, named)
            elif isinstance(value, list):
                items = _list_of_text(value)
                if items:
                    out.setdefault(key, items)
            else:
                out.setdefault(key, value)
        for value in data.values():
            # Descend into every list, exactly as before keeping lists under
            # their own key was added. Skipping the ones already read as text
            # looked tidier and swallowed `@graph` whole — a page with three
            # JSON-LD blocks arrives as {"@graph": [...]}, each block has a
            # `name`, so the entire graph read as a list of names and every
            # field went missing. Measured live on a recipe page, 20 Sep 2026.
            if isinstance(value, dict | list):
                for key, nested in _flatten(value, prefix).items():
                    out.setdefault(key, nested)
    elif isinstance(data, list):
        for item in data:
            for key, value in _flatten(item, prefix).items():
                out.setdefault(key, value)
    return out


def missing_required(data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    return [name for name in schema.get("required", []) if name not in data or data[name] is None]


def coverage(data: dict[str, Any], schema: dict[str, Any]) -> float:
    """The share of the schema's own properties that actually came back.

    `confidence` used to be a constant per SOURCE, so a schema asking for a
    title against a page with no structured markup returned `{}` at 0.95 —
    nothing found, presented as near-certainty (measured on
    quotes.toscrape.com, 9 Sep 2026). A caller reading that number cannot tell
    "we are sure the page says this" from "we are sure of nothing".

    A schema with no declared properties asks for the whole object and there
    is nothing to count, so it scores 1.0 and the source confidence stands.
    """
    props = list((schema.get("properties") or {}).keys())
    if not props:
        return 1.0
    return sum(1 for name in props if data.get(name) is not None) / len(props)


NOTHING_FOUND = (
    "None of the fields in the schema appear on this page, in its structured markup or in its text."
)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

# A model call is injected rather than imported so the extraction path stays
# testable without a provider, and so the engine has no hard LLM dependency.
ModelCaller = Callable[[str, dict[str, Any], str | None], dict[str, Any]]


def extract_against_schema(
    markdown: str,
    structured_hints: dict[str, Any] | None,
    schema: dict[str, Any],
    prompt: str | None = None,
    model: ModelCaller | None = None,
) -> ExtractionOutcome:
    """Structured markup first, model only if it does not answer the schema."""
    if schema.get("type") not in (None, "object"):
        return ExtractionOutcome(error="The top-level schema must describe an object.")

    data = from_markup(structured_hints, schema)
    still_missing = missing_required(data, schema)

    found = coverage(data, schema)
    if not still_missing and found:
        errors = validate(data, schema)
        if not errors:
            # Source confidence times how much of the schema was answered. The
            # two multiply because they are different doubts: how much we trust
            # where it came from, and how much of what was asked for is there.
            return ExtractionOutcome(data=data, confidence=round(0.95 * found, 2), source="markup")

    if model is None:
        # No model configured: report honestly rather than returning a partial
        # object that looks complete.
        if still_missing:
            return ExtractionOutcome(
                error=(
                    "Required fields could not be found in the page's structured "
                    f"markup: {', '.join(still_missing)}. No model extractor is "
                    "configured, so nothing was inferred."
                ),
                data=data or None,
            )
        if not found:
            # Nothing at all. An empty object is not an extraction, and
            # returning one with a confidence beside it says the opposite.
            return ExtractionOutcome(error=NOTHING_FOUND)
        return ExtractionOutcome(data=data, confidence=round(0.9 * found, 2), source="markup")

    # Model pass, then validate. On failure, retry ONCE with the errors fed
    # back, then fail — never return non-conforming output as success.
    attempt_prompt = prompt or ""
    for attempt in range(2):
        try:
            raw = model(markdown, schema, attempt_prompt)
        except Exception as exc:  # noqa: BLE001 - a provider failure is an error, not a crash
            return ExtractionOutcome(error=f"The extraction model failed: {exc}")

        merged = {**data, **(raw or {})}
        merged = coerce(merged, schema)
        errors = validate(merged, schema)
        if not errors:
            model_found = coverage(merged, schema)
            if not model_found:
                return ExtractionOutcome(error=NOTHING_FOUND, source="model")
            return ExtractionOutcome(
                data=merged, confidence=round(0.75 * model_found, 2), source="model"
            )

        if attempt == 0:
            attempt_prompt = (
                f"{prompt or ''}\n\nThe previous attempt did not conform to the schema. "
                f"Fix exactly these problems and return only conforming JSON:\n"
                + "\n".join(f"- {e}" for e in errors)
            )
        else:
            return ExtractionOutcome(
                error=(
                    "The extracted data did not match the schema after a retry. "
                    "Treat this as the page genuinely not containing these fields."
                ),
                validation_errors=errors,
            )
    return ExtractionOutcome(error="Extraction failed.")
