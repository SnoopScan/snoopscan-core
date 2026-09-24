"""In the published core, every hosted-only agent tool answers, never crashes.

The open core ships without the lead-gen pipeline and the platform shortcuts.
Most tools already said "not available on this deployment" when those modules
were missing; findContacts imported its module unguarded and would have raised
(found checking the public README's claims, Sep 2026). The modules are hidden
here the way the export hides them: the import fails.
"""

from __future__ import annotations

import sys

import pytest

from engine.mcp import server

HIDDEN = (
    "engine.leadgen",
    "engine.leadgen.discovery",
    "engine.leadgen.firmographics",
    "engine.leadgen.hiring",
    "engine.leadgen.people",
    "engine.leads",
    "engine.leads.models",
    "engine.platforms",
    "engine.platforms.service",
)


@pytest.fixture
def open_core(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in HIDDEN:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setattr(server, "_credits_refusal", lambda: None)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("findContacts", {"url": "https://example.com"}),
        ("company", {"url": "https://example.com"}),
        ("people", {"company": "Example"}),
        ("hiring", {"url": "https://example.com"}),
        ("findLeads", {"who": "roofers", "where": "Houston, TX"}),
        ("listProducts", {"url": "https://shop.example.com"}),
        ("listPosts", {"url": "https://blog.example.com"}),
    ],
)
async def test_a_hosted_only_tool_says_so(open_core: None, tool: str, kwargs: dict) -> None:
    fn = getattr(server, tool)
    fn = getattr(fn, "fn", fn)
    out = await fn(**kwargs)
    assert "not available on this deployment" in out, out
