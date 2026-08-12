from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from ..logger import get_logger
from ..types import AgentConfig
from .client import MCPServer

logger = get_logger("mcp")


class MCPManager:
    """Owns every MCP server belonging to one agent.

    Servers are started once at boot and reused for every delegation, so agents do
    not pay process-spawn cost per query. A server that fails to start is logged and
    dropped: the agent keeps running with whatever tools did come up.
    """

    def __init__(self, agent: AgentConfig):
        self.agent = agent
        self._servers: dict[str, MCPServer] = {}
        self._tool_to_server: dict[str, str] = {}

    async def connect(self, stack: AsyncExitStack) -> None:
        for config in self.agent.enabled_mcp_servers:
            server = MCPServer(config, self.agent.path)
            try:
                await server.connect(stack)
            except Exception as exc:
                logger.error(
                    "Agent '%s': MCP server '%s' failed to start: %s",
                    self.agent.name,
                    config.name,
                    exc,
                )
                continue

            self._servers[config.name] = server
            for tool_name in server.tool_names:
                if tool_name in self._tool_to_server:
                    logger.warning(
                        "Agent '%s': tool '%s' is exposed by both '%s' and '%s'; keeping '%s'",
                        self.agent.name,
                        tool_name,
                        self._tool_to_server[tool_name],
                        config.name,
                        self._tool_to_server[tool_name],
                    )
                    continue
                self._tool_to_server[tool_name] = config.name

            logger.info(
                "Agent '%s': MCP server '%s' ready with %d tool(s)",
                self.agent.name,
                config.name,
                len(server.tool_names),
            )

    def tools(self) -> list[dict[str, Any]]:
        """Tool schemas for every connected server, deduplicated by name."""
        schemas: list[dict[str, Any]] = []
        for name, server in self._servers.items():
            for schema in server.tools:
                if self._tool_to_server.get(schema["function"]["name"]) == name:
                    schemas.append(schema)
        return schemas

    def owns(self, tool_name: str) -> bool:
        return tool_name in self._tool_to_server

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        server = self._servers[self._tool_to_server[tool_name]]
        return await server.call(tool_name, arguments)
