from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Iterable

from ..logger import get_logger
from ..mcp import MCPManager
from ..parsers import discover_agents
from ..tools import ToolFilter, ToolSet, spec_for
from ..types import AgentConfig, ToolConfig
from .script_runner import ScriptLoadError, ScriptRunner, load_entry_point

logger = get_logger("registry")


def build_toolsets(
    tools: Iterable[ToolConfig], root: Path, owner: str
) -> list[tuple[ToolSet, ToolFilter]]:
    """Instantiate one native toolset per `tools:` entry, each with its own filter.

    Per-entry instances matter: two agents with a `shell` must not share an allowlist, and a
    toolset that holds a resource must not hand it to another agent. A toolset whose dependencies are
    missing is logged and skipped, the same way a failed MCP server is — one unavailable
    capability should not stop the agent loading.
    """
    built: list[tuple[ToolSet, ToolFilter]] = []
    for tool in tools:
        spec = spec_for(tool.name)
        if spec is None:  # pragma: no cover - the parser already filtered these
            continue
        try:
            factory = spec.load()
        except ImportError as exc:
            logger.error("%s: tool '%s' is unavailable — %s", owner, tool.name, exc)
            continue
        try:
            instance = factory(root, tool.settings)
        except Exception as exc:
            logger.error("%s: tool '%s' could not be built — %s", owner, tool.name, exc)
            continue
        built.append((instance, ToolFilter(include=tool.include, exclude=tool.exclude)))
    return built


class ToolBox:
    """The tools one agent — or the orchestrator — can call.

    Native toolsets first, then MCP servers. Native wins a name collision, because a
    third-party server should not be able to shadow `file_delete` with something else.
    """

    def __init__(self, toolsets: list[tuple[ToolSet, ToolFilter]], mcp: MCPManager | None = None):
        self.toolsets = toolsets
        self.mcp = mcp
        self.owner = getattr(getattr(mcp, "agent", None), "name", "orchestrator")
        self._schemas = self._build_schemas()

    def _build_schemas(self) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        native: set[str] = set()

        for toolset, tool_filter in self.toolsets:
            for schema in tool_filter.apply(toolset.schemas()):
                name = schema["function"]["name"]
                if name in native:
                    continue
                native.add(name)
                schemas.append(schema)

        for schema in self.mcp.tools() if self.mcp else []:
            name = schema["function"]["name"]
            if name in native:
                logger.warning(
                    "%s: MCP tool '%s' collides with a native tool; the native one wins",
                    self.owner,
                    name,
                )
                continue
            schemas.append(schema)
        return schemas

    def schemas(self) -> list[dict[str, Any]]:
        return self._schemas

    def offers(self, tool_name: str) -> bool:
        """Whether this toolbox advertises a tool — filtered names are not offered."""
        return any(schema["function"]["name"] == tool_name for schema in self._schemas)

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if not self.offers(tool_name):
            # Excluded by config, so refusing here is the point: a model that guessed the
            # name must not reach past the filter.
            return f"[error] unknown tool '{tool_name}'"
        for toolset, _ in self.toolsets:
            if toolset.owns(tool_name):
                return await toolset.call(tool_name, arguments)
        if self.mcp and self.mcp.owns(tool_name):
            return await self.mcp.call(tool_name, arguments)
        return f"[error] unknown tool '{tool_name}'"

    async def aclose(self) -> None:
        """Release anything a native toolset holds. MCP is closed by the registry's stack."""
        for toolset, _ in self.toolsets:
            try:
                await toolset.aclose()
            except Exception as exc:  # pragma: no cover - shutdown is best-effort
                logger.debug("%s: closing a toolset failed: %s", self.owner, exc)


class Registry:
    """The central registry of agents and their tools.

    Built once at startup. `llm` agents get MCP servers; `script` agents get their module
    imported. Both are offered to the orchestrator as delegation tools unless a script
    agent sets `avoid_orchestrator: true`. A script agent that names a `triggerPoint` also
    runs on its own, in that phase.
    """

    def __init__(self, agents: list[AgentConfig]):
        self.agents = agents
        self.llm_agents = [agent for agent in agents if agent.is_llm]
        # Highest priority first, then by name so a run is reproducible.
        self.script_agents = sorted(
            (agent for agent in agents if agent.is_script),
            key=lambda agent: (-agent.priority, agent.name),
        )
        self.script_agents_before = [
            agent for agent in self.script_agents if agent.runs_before_orchestrator
        ]
        self.script_agents_after = [
            agent for agent in self.script_agents if agent.runs_after_orchestrator
        ]
        self.delegatable_agents = [agent for agent in agents if agent.delegatable]
        self._stack = AsyncExitStack()
        self._toolboxes: dict[str, ToolBox] = {}
        self._script_runners: dict[str, ScriptRunner] = {}
        self._by_tool_name = self._index_by_tool_name()

    def _index_by_tool_name(self) -> dict[str, AgentConfig]:
        """Map every delegation tool name to its agent.

        Agent names are already unique, but slugifying can collapse two of them onto one
        tool name ("build report" and "build-report"), and the orchestrator would then
        route both calls to whichever won. Keep the first and say so.
        """
        index: dict[str, AgentConfig] = {}
        for agent in self.delegatable_agents:
            existing = index.get(agent.tool_name)
            if existing is not None:
                logger.warning(
                    "Agents '%s' and '%s' both map to the tool name '%s'; only '%s' will "
                    "be delegatable. Rename one of them.",
                    existing.name,
                    agent.name,
                    agent.tool_name,
                    existing.name,
                )
                continue
            index[agent.tool_name] = agent
        return index

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
            self._toolboxes[agent.name] = ToolBox(
                build_toolsets(agent.enabled_tools, agent.path, f"Agent '{agent.name}'"),
                manager,
            )

        for agent in self.script_agents:
            try:
                entry_point = load_entry_point(agent)
            except ScriptLoadError as exc:
                # Consistent with a failed MCP server: log it and carry on without it.
                logger.error("Script agent '%s' will not run: %s", agent.name, exc)
                continue
            self._script_runners[agent.name] = ScriptRunner(agent, entry_point)
            if agent.runs_automatically:
                fires = f"{agent.trigger_point} on {agent.trigger_rule or 'every message'}"
            else:
                fires = "delegation only"
            logger.info(
                "Loaded script agent '%s' (priority %s, %s, send_output=%s, orchestrator=%s)",
                agent.name,
                agent.priority,
                fires,
                str(agent.send_output).lower(),
                "delegatable" if agent.delegatable else "hidden",
            )

        self._warn_on_undeliverable_output()
        self._warn_on_unconditional_delegatable_scripts()

    def _warn_on_undeliverable_output(self) -> None:
        """Flag script output that has nowhere to go.

        With no llm agents the orchestrator never runs, so a script agent that also has
        `send_output` disabled produces a string that reaches neither the user nor a
        model. That is almost always a misconfiguration.
        """
        if self.llm_agents:
            return

        automatic = [agent for agent in self.script_agents if agent.runs_automatically]
        stranded = [agent.name for agent in automatic if not agent.send_output]
        if stranded:
            logger.warning(
                "No 'llm' agents are registered, so the orchestrator will not run. "
                "These script agents have send_output disabled, so their output goes "
                "nowhere: %s",
                ", ".join(stranded),
            )

        # Delegation is the only way into these, and nothing will ever delegate.
        unreachable = [
            agent.name for agent in self.script_agents if not agent.runs_automatically
        ]
        if unreachable:
            logger.warning(
                "No 'llm' agents are registered, so nothing will ever delegate. These "
                "script agents have no 'triggerPoint' either, so they can never run: %s",
                ", ".join(unreachable),
            )

    def _warn_on_unconditional_delegatable_scripts(self) -> None:
        """Flag script agents that both fire on every message and are delegatable.

        A `triggerPoint` with no `triggerRule` runs unconditionally, so the orchestrator can
        call an agent that has already run for this very message — usually a duplicate side
        effect. Either add a rule or hide it from the orchestrator.
        """
        if not self.llm_agents:
            return
        both = [
            agent.name
            for agent in self.script_agents
            if agent.delegatable and agent.runs_automatically and agent.trigger_rule is None
        ]
        if both:
            logger.warning(
                "These script agents have a triggerPoint but no triggerRule, so they run "
                "on every message, and are also offered to the orchestrator, which may "
                "call them a second time for the same message: %s. Add a triggerRule, or "
                "set avoid_orchestrator: true.",
                ", ".join(both),
            )

    async def aclose(self) -> None:
        """Shut everything down. Must run in the task that called create().

        Native toolsets first, in case one is holding a resource, then the MCP transports,
        whose contexts have to close in the task that opened them.
        """
        for toolbox in self._toolboxes.values():
            await toolbox.aclose()
        await self._stack.aclose()

    @property
    def has_llm_agents(self) -> bool:
        """Whether the orchestrator has anything to route to."""
        return bool(self.llm_agents)

    def script_runners(self) -> dict[str, ScriptRunner]:
        return self._script_runners

    def script_runner_for(self, agent: AgentConfig) -> ScriptRunner | None:
        """The loaded runner for a script agent, or None if its import failed."""
        return self._script_runners.get(agent.name)

    def delegation_tools(self) -> list[dict[str, Any]]:
        """One function tool per delegatable agent.

        Script agents appear here too unless they set `avoid_orchestrator: true`. They are
        described as deterministic so the model does not expect one to negotiate about the
        task the way an `llm` agent would.
        """
        return [self._delegation_tool(agent) for agent in self._by_tool_name.values()]

    @staticmethod
    def _delegation_tool(agent: AgentConfig) -> dict[str, Any]:
        if agent.is_script:
            how = (
                "This agent runs a fixed script, not a model. It does the one thing it "
                "was written to do and will not adapt to instructions, so call it when "
                "its description matches what is needed. Put anything it may act on in "
                "'task'."
            )
        else:
            how = (
                "Call this to delegate one self-contained task to this agent. The agent "
                "cannot see the conversation, so state everything it needs in 'task'."
            )
        return {
            "type": "function",
            "function": {
                "name": agent.tool_name,
                "description": f"{agent.description}\n\n{how}",
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

        Goes into the orchestrator's system prompt, so the first section lists exactly what
        it can delegate to. The second section covers script agents and how each is reached
        — worth stating, because one that runs on its own may deliver output the model never
        asked for.
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
        for agent in self.script_agents:
            if agent.delegatable:
                lines.append(
                    f"- {agent.name} (tool: {agent.tool_name}) — {agent.description} "
                    f"[deterministic script]"
                )

        if self.script_agents:
            if lines:
                lines.append("")
            lines.append("Script agents (deterministic, no model), and how each is reached:")
            for agent in self.script_agents:
                if agent.runs_automatically:
                    notes = [
                        f"runs {agent.trigger_point}",
                        f"priority {agent.priority}",
                        f"trigger: {agent.trigger_rule or 'always'}",
                    ]
                else:
                    notes = ["no triggerPoint, so it runs only when delegated to"]
                if agent.send_output:
                    notes.append("send_output")
                if not agent.delegatable:
                    notes.append("not delegatable")
                if agent.name not in self._script_runners:
                    notes.append("FAILED TO LOAD")
                lines.append(
                    f"- {agent.name} — {agent.description} [{', '.join(notes)}]"
                )

        return "\n".join(lines)
