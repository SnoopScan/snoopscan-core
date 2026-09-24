"""The model half of /v1/extract, driven against a fake SDK."""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import httpx
import pytest

from engine.core.extract import model as m


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Usage:
    input_tokens = 10
    output_tokens = 5


class _Response:
    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.content = [_Block(text)]
        self.stop_reason = stop_reason
        self.model = "claude-opus-5"
        self.usage = _Usage()


def _fake_sdk(
    monkeypatch: pytest.MonkeyPatch, reply: str, stop_reason: str = "end_turn"
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class Messages:
        def create(self, **kwargs: Any) -> _Response:
            calls.append(kwargs)
            return _Response(reply, stop_reason)

    class Beta:
        messages = Messages()

    class Anthropic:
        def __init__(self, **kwargs: Any) -> None:
            self.beta = Beta()

    mod = types.ModuleType("anthropic")
    mod.Anthropic = Anthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    return calls


def test_unconfigured_means_no_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert m.is_configured() is False
    assert m.model_caller() is None


def test_the_call_carries_schema_prompt_and_page_and_returns_the_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_sdk(monkeypatch, 'Sure:\n```json\n{"title": "Kettle", "price": 29.5}\n```')
    caller = m.model_caller()
    assert caller is not None
    schema = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}
    out = caller("# Kettle\n\nA kettle for $29.50", schema, "The product")
    assert out == {"title": "Kettle", "price": 29.5}
    sent = calls[0]
    assert sent["model"] == m.settings.extract_model
    assert sent["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in sent["betas"]
    user = sent["messages"][0]["content"]
    assert (
        json.dumps(schema) in user
        and "Instructions: The product" in user
        and "A kettle for $29.50" in user
    )
    assert sent["system"] == m.SYSTEM


def test_the_page_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_sdk(monkeypatch, "{}")
    monkeypatch.setattr(m.settings, "extract_max_chars", 20)
    caller = m.model_caller()
    assert caller is not None
    caller("x" * 1000, {"type": "object"}, None)
    assert calls[0]["messages"][0]["content"].endswith("Page:\n" + "x" * 20)


def test_a_refusal_is_an_error_not_data(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_sdk(monkeypatch, "", stop_reason="refusal")
    caller = m.model_caller()
    assert caller is not None
    with pytest.raises(RuntimeError, match="declined"):
        caller("page", {"type": "object"}, None)


def test_no_object_in_the_reply_is_an_error() -> None:
    with pytest.raises(ValueError):
        m.first_object("I could not find anything.")
    with pytest.raises(ValueError):
        m.first_object("[1, 2]")
    assert m.first_object('noise {"a": 1} noise') == {"a": 1}


def test_a_customer_key_is_used_for_that_call_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls: list[dict[str, Any]] = []
    seen_keys: list[str] = []

    class Messages:
        def create(self, **kwargs: Any) -> _Response:
            calls.append(kwargs)
            return _Response('{"title": "Kettle"}')

    class Beta:
        messages = Messages()

    class Anthropic:
        def __init__(self, **kwargs: Any) -> None:
            seen_keys.append(kwargs.get("api_key", ""))
            self.beta = Beta()

    mod = types.ModuleType("anthropic")
    mod.Anthropic = Anthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", mod)

    assert m.model_caller() is None  # nothing of the operator's
    caller = m.model_caller(m.Spec("anthropic", "sk-ant-customer", "claude-sonnet-5"))
    assert caller is not None
    assert caller("page", {"type": "object"}, None) == {"title": "Kettle"}
    assert seen_keys == ["sk-ant-customer"] and calls[0]["model"] == "claude-sonnet-5"


def test_openai_goes_over_plain_http_in_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, Any] = {}

    class R:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = ""

        def json(self) -> dict[str, Any]:
            return {
                "model": "gpt-4o-mini",
                "choices": [{"message": {"content": '{"title": "Kettle"}'}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4},
            }

    def fake_post(url: str, **kwargs: Any) -> R:
        sent.update({"url": url, **kwargs})
        return R()

    monkeypatch.setattr(httpx, "post", fake_post)
    caller = m.model_caller(m.Spec("openai", "sk-openai-customer", None))
    assert caller is not None
    assert caller("# Kettle", {"type": "object"}, "The product") == {"title": "Kettle"}
    assert sent["url"].endswith("/v1/chat/completions")
    assert sent["headers"]["Authorization"] == "Bearer sk-openai-customer"
    assert (
        sent["json"]["response_format"] == {"type": "json_object"}
        and sent["json"]["model"] == m.settings.openai_extract_model
    )
    assert "Instructions: The product" in sent["json"]["messages"][1]["content"]


def test_openai_errors_are_reported_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    class R:
        status_code = 401
        headers = {"content-type": "application/json"}
        text = "{}"

        def json(self) -> dict[str, Any]:
            return {"error": {"message": "Incorrect API key provided"}}

    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: R())
    caller = m.model_caller(m.Spec("openai", "sk-bad", None))
    assert caller is not None
    with pytest.raises(RuntimeError, match="401"):
        caller("page", {"type": "object"}, None)


def test_the_spec_never_prints_its_key() -> None:
    from engine.core.models import ModelSpec

    spec = ModelSpec(provider="anthropic", apiKey="sk-ant-secret-value")
    assert "secret" not in repr(spec) and "***" in repr(spec)
