"""Live MCP integration: starts a real stdio MCP server as a subprocess."""

from __future__ import annotations

import sys
import textwrap
from contextlib import AsyncExitStack

import pytest

from stark.mcp import MCPManager
from stark.orchestration import Registry
from stark.parsers import parse_agent_file

pytestmark = pytest.mark.asyncio

SERVER = textwrap.dedent(
    '''
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("test-server")

    @mcp.tool()
    def echo(text: str) -> str:
        """Echo the text back."""
        return f"echo: {text}"

    @mcp.tool()
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    @mcp.tool()
    def secret() -> str:
        """A tool that should be filterable."""
        return "classified"

    if __name__ == "__main__":
        mcp.run()
    '''
).strip()

AGENT_MD = """\
---
name: mcp-agent
description: Talks to a local MCP server.
provider: anthropic
model: claude-opus-5
mcp:
  - name: local
    enable: true
    command: {python}
    args: ["{server}"]
{extra}
---

Use the MCP tools.
"""


def build_agent(tmp_path, extra: str = ""):
    directory = tmp_path / "mcp-agent"
    directory.mkdir(parents=True, exist_ok=True)
    server = directory / "server.py"
    server.write_text(SERVER, encoding="utf-8")
    (directory / "AGENT.md").write_text(
        AGENT_MD.format(python=sys.executable, server=server, extra=extra),
        encoding="utf-8",
    )
    return parse_agent_file(directory / "AGENT.md")


async def test_connects_lists_and_calls_tools(tmp_path):
    agent = build_agent(tmp_path)
    manager = MCPManager(agent)

    async with AsyncExitStack() as stack:
        await manager.connect(stack)

        names = {tool["function"]["name"] for tool in manager.tools()}
        assert {"echo", "add", "secret"} <= names

        schema = next(t for t in manager.tools() if t["function"]["name"] == "add")
        assert schema["function"]["parameters"]["properties"].keys() >= {"a", "b"}

        assert manager.owns("echo")
        assert not manager.owns("nope")
        assert "echo: hi" in await manager.call("echo", {"text": "hi"})
        assert "7" in await manager.call("add", {"a": 3, "b": 4})


async def test_exclude_filters_tools(tmp_path):
    agent = build_agent(tmp_path, extra='    exclude: ["secret"]')
    manager = MCPManager(agent)

    async with AsyncExitStack() as stack:
        await manager.connect(stack)
        names = {tool["function"]["name"] for tool in manager.tools()}

    assert "echo" in names
    assert "secret" not in names


async def test_include_is_an_allowlist(tmp_path):
    agent = build_agent(tmp_path, extra='    include: ["echo"]')
    manager = MCPManager(agent)

    async with AsyncExitStack() as stack:
        await manager.connect(stack)
        names = {tool["function"]["name"] for tool in manager.tools()}

    assert names == {"echo"}


async def test_registry_merges_mcp_and_workspace_tools(tmp_path):
    build_agent(tmp_path)
    registry = await Registry.create(tmp_path)
    try:
        agent = registry.agents[0]
        names = {tool["function"]["name"] for tool in registry.toolbox_for(agent).schemas()}
        assert {"workspace_list", "workspace_read", "workspace_run"} <= names
        assert {"echo", "add"} <= names

        toolbox = registry.toolbox_for(agent)
        assert "echo: routed" in await toolbox.call("echo", {"text": "routed"})
        assert "server.py" in await toolbox.call("workspace_list", {})
    finally:
        await registry.aclose()


async def test_failed_server_does_not_break_the_agent(tmp_path, caplog):
    directory = tmp_path / "mcp-agent"
    directory.mkdir(parents=True)
    (directory / "AGENT.md").write_text(
        """\
---
name: mcp-agent
description: Points at a command that does not exist.
provider: anthropic
model: claude-opus-5
mcp:
  - name: missing
    enable: true
    command: definitely-not-a-real-binary-xyz
---

Body.
""",
        encoding="utf-8",
    )

    with caplog.at_level("ERROR"):
        registry = await Registry.create(tmp_path)

    try:
        agent = registry.agents[0]
        names = {tool["function"]["name"] for tool in registry.toolbox_for(agent).schemas()}
        # The agent still loads with its workspace tools.
        assert names == {"workspace_list", "workspace_read", "workspace_run"}
        assert "failed to start" in caplog.text
    finally:
        await registry.aclose()


async def test_tools_are_callable_from_concurrent_child_tasks(tmp_path):
    """Sessions are opened in the startup task but used from gather() children.

    This is exactly what parallel delegation does, so exercise it directly.
    """
    import asyncio

    build_agent(tmp_path)
    registry = await Registry.create(tmp_path)
    try:
        agent = registry.agents[0]
        toolbox = registry.toolbox_for(agent)

        results = await asyncio.gather(
            *(toolbox.call("echo", {"text": f"n{index}"}) for index in range(8))
        )
        assert [f"echo: n{index}" in result for index, result in enumerate(results)] == [True] * 8

        mixed = await asyncio.gather(
            toolbox.call("add", {"a": 1, "b": 2}),
            toolbox.call("workspace_list", {}),
            toolbox.call("echo", {"text": "mixed"}),
        )
        assert "3" in mixed[0]
        assert "server.py" in mixed[1]
        assert "echo: mixed" in mixed[2]
    finally:
        await registry.aclose()


async def test_disabled_server_is_never_started(tmp_path):
    """`enable: false` must skip the subprocess entirely, not just hide its tools."""
    directory = tmp_path / "mcp-agent"
    directory.mkdir(parents=True)
    server = directory / "server.py"
    server.write_text(SERVER, encoding="utf-8")
    # A second entry pointing at a command that would fail loudly if it ever ran.
    (directory / "AGENT.md").write_text(
        f"""\
---
name: mcp-agent
description: One enabled server and one parked one.
provider: anthropic
model: claude-opus-5
mcp:
  - name: live
    enable: true
    command: {sys.executable}
    args: ["{server}"]
  - name: parked
    enable: false
    command: definitely-not-a-real-binary-xyz
---

Body.
""",
        encoding="utf-8",
    )

    agent = parse_agent_file(directory / "AGENT.md")
    assert [item.name for item in agent.mcp] == ["live", "parked"]
    assert [item.name for item in agent.enabled_mcp_servers] == ["live"]

    manager = MCPManager(agent)
    async with AsyncExitStack() as stack:
        # No error is logged for 'parked' because it is never attempted.
        await manager.connect(stack)
        names = {tool["function"]["name"] for tool in manager.tools()}

    assert {"echo", "add"} <= names
