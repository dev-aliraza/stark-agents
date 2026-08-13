from __future__ import annotations

import shutil
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.types import Tool

from ..errors import MCPError
from ..logger import get_logger
from ..types import MCPServerConfig

logger = get_logger("mcp")


def _tool_schema(tool: Tool) -> dict[str, Any]:
    """Convert an MCP tool declaration into an OpenAI-style function schema."""
    function: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description or "",
    }
    # mcp 2.x exposes this as `input_schema`; `inputSchema` is only the wire alias.
    schema = tool.input_schema or {}
    if schema.get("properties"):
        function["parameters"] = schema
    return {"type": "function", "function": function}


def _describe(exc: BaseException) -> str:
    """Render an exception as one line, flattening anyio's ExceptionGroups.

    A transport failure arrives wrapped in nested groups whose default text is just
    "unhandled errors in a TaskGroup", which says nothing useful in a log.
    """
    leaves: list[str] = []
    seen: set[int] = set()

    def walk(error: BaseException) -> None:
        if id(error) in seen:
            return
        seen.add(id(error))
        nested = getattr(error, "exceptions", None)
        if nested:
            for child in nested:
                walk(child)
            return
        text = str(error).strip()
        leaves.append(f"{type(error).__name__}: {text}" if text else type(error).__name__)

    walk(exc)
    unique = list(dict.fromkeys(leaves))
    return "; ".join(unique) or type(exc).__name__


def _filter(tools: list[Tool], include: list[str], exclude: list[str]) -> list[Tool]:
    """Apply include/exclude filters. `include` wins when both are set."""
    if include:
        allowed = set(include)
        return [tool for tool in tools if tool.name in allowed]
    if exclude:
        denied = set(exclude)
        return [tool for tool in tools if tool.name not in denied]
    return tools


class MCPServer:
    """A single connected MCP server, held open for the process lifetime."""

    def __init__(self, config: MCPServerConfig, agent_dir: Path):
        self.config = config
        self.agent_dir = agent_dir
        self.session: ClientSession | None = None
        self.tools: list[dict[str, Any]] = []

    async def connect(self, stack: AsyncExitStack) -> None:
        """Start the transport, initialize the session and list its tools.

        Each server is built on its own exit stack, which is handed to the caller's
        stack only once the handshake succeeds. That matters because a transport can
        fail asynchronously — an unreachable HTTP endpoint surfaces its error when the
        transport's task group unwinds, not when it is entered. Keeping a failed
        server's contexts off the shared stack means the failure lands here, where it
        can be logged and the server dropped, instead of at process shutdown.

        Raises `MCPError`, or whatever the transport raised, on failure.
        """
        own = AsyncExitStack()
        await own.__aenter__()

        try:
            read_stream, write_stream = await self._open_transport(own)

            session = await own.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()

            listed = (await session.list_tools()).tools
        except BaseException:
            # Unwind just this server. When a transport dies in a background task, anyio
            # cancels us — so the exception we caught can be a bare CancelledError while
            # the real cause only surfaces as the task group unwinds during aclose().
            try:
                await own.aclose()
            except BaseException as teardown:
                raise MCPError(_describe(teardown)) from teardown
            raise

        # Handshake succeeded, so the connection may outlive this call.
        stack.push_async_callback(own.aclose)

        self.session = session
        self.tools = [_tool_schema(tool) for tool in _filter(
            listed, self.config.include, self.config.exclude
        )]

    async def _open_transport(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        """Start the configured transport and return its read/write streams."""
        if self.config.transport == "streamable_http":
            # `streamable_http_client` takes no `headers` argument — HTTP settings belong
            # to the client you hand it. When we build one, we also own closing it.
            http_client = None
            if self.config.headers:
                http_client = await stack.enter_async_context(
                    create_mcp_http_client(headers=self.config.headers)
                )
            transport = await stack.enter_async_context(
                streamable_http_client(self.config.url or "", http_client=http_client)
            )
        else:
            command = self.config.command or ""
            if not shutil.which(command):
                raise MCPError(f"command '{command}' not found on PATH")
            transport = await stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=command,
                        args=self.config.args,
                        env={**get_default_environment(), **self.config.env},
                        cwd=str(self.agent_dir),
                    )
                )
            )

        return transport[0], transport[1]

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Invoke a tool and flatten its result into text for the model."""
        if self.session is None:
            raise MCPError(f"server '{self.config.name}' is not connected")

        result = await self.session.call_tool(tool_name, arguments or {})
        parts: list[str] = []
        for block in result.content or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
                continue
            data = getattr(block, "data", None)
            if data is not None:
                parts.append(str(data))
                continue
            parts.append(str(block))

        rendered = "\n".join(part for part in parts if part).strip()
        if getattr(result, "isError", False):
            return f"[tool error] {rendered or 'the server reported an error with no detail'}"
        return rendered or "Tool executed successfully (no output returned)."

    @property
    def tool_names(self) -> list[str]:
        return [tool["function"]["name"] for tool in self.tools]
