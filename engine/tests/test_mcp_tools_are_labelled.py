"""Every agent tool carries the title and labels AI app directories require.

Claude's connector directory rejects a server whose tools lack a title and the
read-only / destructive labels; ChatGPT's requires all three hints. The server
also presents itself by the product's name and a real version, not the
repository's name ("scraping-engine", version unset) it used to report.
"""

from __future__ import annotations

import pytest

from engine.mcp import server

# Tools that change something: start a paid background job, or store a new
# baseline. Everything else only reads.
WRITES = {"crawl", "findLeads", "checkChanges"}
# Tools that only read our own job records, not the web.
CLOSED_WORLD = {"fetchMore", "crawlStatus", "crawlPages", "leadsStatus"}


@pytest.mark.anyio
async def test_every_tool_has_a_title_and_honest_labels() -> None:
    tools = await server.mcp.list_tools()
    assert len(tools) >= 18
    for tool in tools:
        assert tool.title, f"{tool.name} has no title"
        hints = tool.annotations
        assert hints is not None, f"{tool.name} has no annotations"
        assert hints.destructive_hint is False, f"{tool.name}: nothing here deletes anything"
        assert hints.read_only_hint is (tool.name not in WRITES), tool.name
        assert hints.open_world_hint is (tool.name not in CLOSED_WORLD), tool.name


def test_the_server_is_named_for_the_product_with_a_real_version() -> None:
    assert server.mcp.name == "SnoopScan"
    assert server.MCP_SERVER_VERSION.count(".") == 2
    assert "only reads" in (server.mcp.instructions or "")
