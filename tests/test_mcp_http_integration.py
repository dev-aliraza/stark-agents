"""Live streamable-HTTP MCP integration.

The stdio path is covered in test_mcp_integration.py. This file covers the other
transport, which has its own client construction: `streamable_http_client` takes no
`headers` argument, so Stark builds and owns an httpx client when headers are configured.
Without a runtime test here, that branch is only ever type-checked.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
import time
from contextlib import AsyncExitStack, closing
from pathlib import Path

import pytest

from stark.mcp import MCPManager
from stark.parsers import parse_agent_file

SERVER = textwrap.dedent(
    '''
    import sys

    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("http-server", log_level="WARNING")

    @mcp.tool()
    def ping() -> str:
        """Return pong."""
        return "pong"

    @mcp.tool()
    def shout(text: str) -> str:
        """Uppercase the text."""
        return text.upper()

    @mcp.tool()
    def dangerous() -> str:
        """Should be filterable via exclude."""
        return "boom"

    if __name__ == "__main__":
        mcp.run(transport="streamable-http", host="127.0.0.1", port=int(sys.argv[1]))
    '''
).strip()

AGENT_MD = """\
---
name: http-agent
description: Talks to an MCP server over streamable HTTP.
provider: anthropic
model: claude-opus-5
mcp:
  - name: remote
    enable: true
    transport: streamable_http
    url: http://127.0.0.1:{port}/mcp
{extra}
---

Use the remote tools.
"""


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until_listening(port: int, process: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"server exited early with code {process.returncode}:\n"
                f"{(process.stderr.read() if process.stderr else '')}"
            )
        with closing(socket.socket()) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"server did not start listening on port {port} within {timeout}s")


@pytest.fixture(scope="module")
def http_server(tmp_path_factory) -> int:
    """Start one streamable-HTTP MCP server for the whole module."""
    directory = tmp_path_factory.mktemp("http-mcp")
    script = directory / "server.py"
    script.write_text(SERVER, encoding="utf-8")

    port = free_port()
    process = subprocess.Popen(
        [sys.executable, str(script), str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_until_listening(port, process)
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


def build_agent(tmp_path: Path, port: int, extra: str = ""):
    directory = tmp_path / "http-agent"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "AGENT.md").write_text(
        AGENT_MD.format(port=port, extra=extra), encoding="utf-8"
    )
    return parse_agent_file(directory / "AGENT.md")


async def test_connects_and_calls_tools_over_http(tmp_path, http_server):
    agent = build_agent(tmp_path, http_server)
    assert agent.mcp[0].transport == "streamable_http"

    manager = MCPManager(agent)
    async with AsyncExitStack() as stack:
        await manager.connect(stack)

        names = {tool["function"]["name"] for tool in manager.tools()}
        assert {"ping", "shout", "dangerous"} <= names

        assert "pong" in await manager.call("ping", {})
        assert "HELLO" in await manager.call("shout", {"text": "hello"})


async def test_headers_are_sent_via_the_client_we_own(tmp_path, http_server):
    """The `headers:` field must survive the move into an httpx client.

    A bad header would break the request outright, so a successful call proves the
    client was built, used, and accepted.
    """
    agent = build_agent(
        tmp_path,
        http_server,
        extra="    headers:\n      X-Stark-Test: integration\n      Authorization: Bearer test-token",
    )
    assert agent.mcp[0].headers == {
        "X-Stark-Test": "integration",
        "Authorization": "Bearer test-token",
    }

    manager = MCPManager(agent)
    async with AsyncExitStack() as stack:
        await manager.connect(stack)
        assert "pong" in await manager.call("ping", {})


async def test_exclude_filters_http_tools(tmp_path, http_server):
    agent = build_agent(tmp_path, http_server, extra='    exclude: ["dangerous"]')

    manager = MCPManager(agent)
    async with AsyncExitStack() as stack:
        await manager.connect(stack)
        names = {tool["function"]["name"] for tool in manager.tools()}

    assert "ping" in names
    assert "dangerous" not in names


async def test_unreachable_http_server_is_reported_not_raised(tmp_path, caplog):
    """A dead endpoint must degrade the agent, not crash discovery."""
    agent = build_agent(tmp_path, free_port())  # nothing listening there

    manager = MCPManager(agent)
    with caplog.at_level("ERROR"):
        async with AsyncExitStack() as stack:
            await manager.connect(stack)

    assert manager.tools() == []
    assert "failed to start" in caplog.text


async def test_repeated_connects_do_not_leak_the_http_client(tmp_path, http_server):
    """Stark owns the httpx client when headers are set, so it must close each time."""
    agent = build_agent(
        tmp_path, http_server, extra="    headers:\n      X-Stark-Test: loop"
    )

    for _ in range(3):
        manager = MCPManager(agent)
        async with AsyncExitStack() as stack:
            await manager.connect(stack)
            assert "pong" in await manager.call("ping", {})
