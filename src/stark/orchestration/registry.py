from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Iterable

from ..logger import get_logger
from ..mcp import MCPManager
from ..parsers import discover_agents
from ..tools import WorkspaceTools, workspace_schemas
from ..types import AgentConfig

logger = get_logger("registry")


class ToolBox:
    """The tools one agent can call: its workspace plus its MCP servers."""

    def __init__(self, workspace: WorkspaceTools, mcp: MCPManager):
        self.workspace = workspace
        self.mcp = mcp
        self._schemas = self._build_schemas()

    def _build_schemas(self) -> list[dict[str, Any]]:
        schemas = list(workspace_schemas())
        builtin = {schema["function"]["name"] for schema in schemas}
        for schema in self.mcp.tools():
            name = schema["function"]["name"]
            if name in builtin:
                logger.warning(
                    "Agent '%s': MCP tool '%s' collides with a built-in workspace tool; "
                    "the built-in wins",
                    self.mcp.agent.name,
                    name,
                )
                continue
            schemas.append(schema)
        return schemas

    def schemas(self) -> list[dict[str, Any]]:
        return self._schemas

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if self.workspace.owns(tool_name):
            return await self.workspace.call(tool_name, arguments)
        if self.mcp.owns(tool_name):
            return await self.mcp.call(tool_name, arguments)
        return f"[error] unknown tool '{tool_name}'"


class Registry:
    """The central registry of agents and their tools.

    Built once at startup: agents are discovered, their MCP servers are started, and
    each agent is exposed to the orchestrator as a single delegation tool.
    """

    def __init__(self, agents: list[AgentConfig]):
        self.agents = agents
        self._stack = AsyncExitStack()
        self._toolboxes: dict[str, ToolBox] = {}
        self._by_tool_name: dict[str, AgentConfig] = {
            agent.tool_name: agent for agent in agents
        }

    @classmethod
    async def create(
        cls,
        agents_dir: str | Path,
        exclude_agents: Iterable[str] | None = None,
    ) -> "Registry":
        registry = cls(discover_agents(agents_dir, exclude_agents))
        await registry._start()
        return registry

    async def _start(self) -> None:
        await self._stack.__aenter__()
        for agent in self.agents:
            manager = MCPManager(agent)
            await manager.connect(self._stack)
            self._toolboxes[agent.name] = ToolBox(WorkspaceTools(agent.path), manager)

    async def aclose(self) -> None:
        """Shut every MCP server down. Must run in the task that called create()."""
        await self._stack.aclose()

    def delegation_tools(self) -> list[dict[str, Any]]:
        """One function tool per agent, for the orchestrator to call."""
        return [
            {
                "type": "function",
                "function": {
                    "name": agent.tool_name,
                    "description": (
                        f"{agent.description}\n\n"
                        "Call this to delegate one self-contained task to this agent. "
                        "The agent cannot see the conversation, so state everything it "
                        "needs in 'task'."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": (
                                    "The complete, self-contained task for this agent, "
                                    "including any detail it needs to act."
                                ),
                            },
                            "context": {
                                "type": "string",
                                "description": (
                                    "Optional supporting context, such as findings from "
                                    "another agent."
                                ),
                            },
                        },
                        "required": ["task"],
                    },
                },
            }
            for agent in self.agents
        ]

    def is_agent_tool(self, tool_name: str) -> bool:
        return tool_name in self._by_tool_name

    def agent_for(self, tool_name: str) -> AgentConfig:
        return self._by_tool_name[tool_name]

    def toolbox_for(self, agent: AgentConfig) -> ToolBox:
        return self._toolboxes[agent.name]

    def roster(self) -> str:
        """A human- and model-readable summary of the loaded agents."""
        if not self.agents:
            return "No agents are currently registered."
        lines = []
        for agent in self.agents:
            tool_count = len(self.toolbox_for(agent).schemas())
            lines.append(
                f"- {agent.name} (tool: {agent.tool_name}) — {agent.description} "
                f"[{agent.provider}/{agent.model}, {tool_count} tools]"
            )
        return "\n".join(lines)
