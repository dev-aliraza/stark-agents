from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Iterable

from ..logger import get_logger
from ..mcp import MCPManager
from ..parsers import discover_agents
from ..tools import WorkspaceTools, workspace_schemas
from ..types import AgentConfig
from .script_runner import ScriptLoadError, ScriptRunner, load_entry_point

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

    Built once at startup. `llm` agents get MCP servers and a delegation tool offered to
    the orchestrator; `script` agents get their module imported and are reachable only
    through the script phase. The two sets are kept separate because a script agent must
    never appear in the orchestrator's tool list.
    """

    def __init__(self, agents: list[AgentConfig]):
        self.agents = agents
        self.llm_agents = [agent for agent in agents if agent.is_llm]
        # Highest priority first, then by name so a run is reproducible.
        self.script_agents = sorted(
            (agent for agent in agents if agent.is_script),
            key=lambda agent: (-agent.priority, agent.name),
        )
        self._stack = AsyncExitStack()
        self._toolboxes: dict[str, ToolBox] = {}
        self._script_runners: dict[str, ScriptRunner] = {}
        self._by_tool_name: dict[str, AgentConfig] = {
            agent.tool_name: agent for agent in self.llm_agents
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

        for agent in self.llm_agents:
            manager = MCPManager(agent)
            await manager.connect(self._stack)
            self._toolboxes[agent.name] = ToolBox(WorkspaceTools(agent.path), manager)

        for agent in self.script_agents:
            try:
                entry_point = load_entry_point(agent)
            except ScriptLoadError as exc:
                # Consistent with a failed MCP server: log it and carry on without it.
                logger.error("Script agent '%s' will not run: %s", agent.name, exc)
                continue
            self._script_runners[agent.name] = ScriptRunner(agent, entry_point)
            logger.info(
                "Loaded script agent '%s' (priority %s, send_output=%s, trigger=%s)",
                agent.name,
                agent.priority,
                str(agent.send_output).lower(),
                agent.trigger_rule or "always",
            )

        self._warn_on_undeliverable_output()

    def _warn_on_undeliverable_output(self) -> None:
        """Flag script output that has nowhere to go.

        With no llm agents the orchestrator never runs, so a script agent that also has
        `send_output` disabled produces a string that reaches neither the user nor a
        model. That is almost always a misconfiguration.
        """
        if self.llm_agents:
            return
        stranded = [agent.name for agent in self.script_agents if not agent.send_output]
        if stranded:
            logger.warning(
                "No 'llm' agents are registered, so the orchestrator will not run. "
                "These script agents have send_output disabled, so their output goes "
                "nowhere: %s",
                ", ".join(stranded),
            )

    async def aclose(self) -> None:
        """Shut every MCP server down. Must run in the task that called create()."""
        await self._stack.aclose()

    @property
    def has_llm_agents(self) -> bool:
        """Whether the orchestrator has anything to route to."""
        return bool(self.llm_agents)

    def script_runners(self) -> dict[str, ScriptRunner]:
        return self._script_runners

    def delegation_tools(self) -> list[dict[str, Any]]:
        """One function tool per `llm` agent. Script agents are never offered."""
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
            for agent in self.llm_agents
        ]

    def is_agent_tool(self, tool_name: str) -> bool:
        return tool_name in self._by_tool_name

    def agent_for(self, tool_name: str) -> AgentConfig:
        return self._by_tool_name[tool_name]

    def toolbox_for(self, agent: AgentConfig) -> ToolBox:
        """The tools available to an `llm` agent.

        Script agents have no toolbox — they call no model, so there is nothing to offer
        one. Asking for theirs is a programming error, and a bare KeyError here is hard to
        trace back to its cause.
        """
        toolbox = self._toolboxes.get(agent.name)
        if toolbox is None:
            if agent.is_script:
                raise KeyError(
                    f"agent '{agent.name}' is a script agent and has no toolbox; "
                    "iterate registry.llm_agents instead of registry.agents"
                )
            raise KeyError(f"no toolbox registered for agent '{agent.name}'")
        return toolbox

    def roster(self) -> str:
        """A human-readable summary of every loaded agent.

        Only the `llm` section is shown to the model — see `delegation_tools`. Script
        agents are listed for the operator's benefit, marked with how they are reached.
        """
        if not self.agents:
            return "No agents are currently registered."

        lines: list[str] = []
        for agent in self.llm_agents:
            tool_count = len(self.toolbox_for(agent).schemas())
            lines.append(
                f"- {agent.name} (tool: {agent.tool_name}) — {agent.description} "
                f"[{agent.provider}/{agent.model}, {tool_count} tools]"
            )

        if self.script_agents:
            if lines:
                lines.append("")
            lines.append("Script agents (run before the orchestrator, never delegated to):")
            for agent in self.script_agents:
                loaded = "" if agent.name in self._script_runners else ", FAILED TO LOAD"
                lines.append(
                    f"- {agent.name} — {agent.description} "
                    f"[priority {agent.priority}, "
                    f"trigger: {agent.trigger_rule or 'always'}"
                    f"{', send_output' if agent.send_output else ''}{loaded}]"
                )

        return "\n".join(lines)
