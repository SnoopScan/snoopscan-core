"""The model half of /v1/extract.

Structured markup answers most schemas for free (structured_json.from_markup).
When required fields are still missing, this asks a model to fill them from
the page's markdown — and only the fields the schema names. The result is
still validated by extract_against_schema before it is returned, so a model
that invents a value that does not conform is an error, not data.

Injected as a ModelCaller rather than imported by the extraction path, so the
core stays testable without a provider and a self-hoster without a key gets
the honest "no model configured" outcome instead of a crash.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from engine.core.extract.structured_json import ModelCaller
from engine.settings import settings

logger = structlog.get_logger(__name__)

SYSTEM = (
    "You extract structured data from one web page. Answer with a single JSON "
    "object that matches the schema exactly. Use null for any field the page "
    "does not contain, and never invent a value. No prose, no code fences."
)


@dataclass(frozen=True)
class Spec:
    provider: str
    api_key: str
    name: str | None = None


def is_configured() -> bool:
    """The operator's own model: a model name and the SDK's API key. Nothing else is read."""
    return bool(settings.extract_model) and bool(os.environ.get("ANTHROPIC_API_KEY"))


def model_caller(spec: Spec | None = None) -> ModelCaller | None:
    """A synchronous caller for extract_against_schema, or None when nothing is configured.

    With a spec, the customer's own key and model are used for this call only.
    Without one, the operator's ANTHROPIC_API_KEY applies, if set. Synchronous
    on purpose: the extraction path is plain Python and the route runs it in
    a worker thread, so a slow model never blocks the event loop.
    """
    if spec is None:
        if not is_configured():
            return None
        spec = Spec("anthropic", os.environ["ANTHROPIC_API_KEY"], settings.extract_model)
    if spec.provider == "anthropic":
        return _anthropic(spec)
    if spec.provider == "openai":
        return _openai(spec)
    raise ValueError(f"unknown model provider {spec.provider!r}")


def _prompt(markdown: str, schema: dict[str, Any], prompt: str | None) -> str:
    page = markdown[: settings.extract_max_chars]
    user = f"Schema:\n{json.dumps(schema)}\n\n"
    if prompt:
        user += f"Instructions: {prompt}\n\n"
    return user + f"Page:\n{page}"


def _anthropic(spec: Spec) -> ModelCaller:
    import anthropic  # deferred: optional at runtime for self-hosters

    client = anthropic.Anthropic(api_key=spec.api_key)
    model = spec.name or settings.extract_model

    def call(markdown: str, schema: dict[str, Any], prompt: str | None) -> dict[str, Any]:
        # Server-side refusal fallback is on by default: a page the primary model
        # declines is retried on the fallback inside the same call.
        # The SDK types these as TypedDicts, which a bare dict literal does
        # not narrow to. Named locals keep the call strictly typed without
        # casting away the checking we actually want here.
        messages: list[anthropic.types.beta.BetaMessageParam] = [
            {"role": "user", "content": _prompt(markdown, schema, prompt)}
        ]
        output_config: anthropic.types.beta.BetaOutputConfigParam = {
            "effort": settings.extract_effort
        }
        response = client.beta.messages.create(
            model=model,
            max_tokens=settings.extract_max_tokens,
            system=SYSTEM,
            messages=messages,
            output_config=output_config,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("the model declined to read this page")
        text = "".join(block.text for block in response.content if block.type == "text")
        logger.info(
            "extract_model_call",
            provider="anthropic",
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return first_object(text)

    return call


def _openai(spec: Spec) -> ModelCaller:
    """OpenAI's chat completions over plain HTTP; JSON mode keeps the reply an object."""
    model = spec.name or settings.openai_extract_model

    def call(markdown: str, schema: dict[str, Any], prompt: str | None) -> dict[str, Any]:
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {spec.api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": _prompt(markdown, schema, prompt)},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": settings.extract_max_tokens,
            },
            timeout=60.0,
        )
        if r.status_code >= 400:
            detail = (
                r.json().get("error", {}).get("message", r.text[:200])
                if r.headers.get("content-type", "").startswith("application/json")
                else r.text[:200]
            )
            raise RuntimeError(f"OpenAI returned {r.status_code}: {detail}")
        body = r.json()
        text = body["choices"][0]["message"]["content"]
        usage = body.get("usage", {})
        logger.info(
            "extract_model_call",
            provider="openai",
            model=body.get("model", model),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )
        return first_object(text)

    return call


def first_object(text: str) -> dict[str, Any]:
    """The first JSON object in a reply, fences and prose tolerated."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("the model returned no JSON object")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("the model returned JSON that is not an object")
    return data
