from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .triggers import TriggerRule

# Agent types. `llm` is the default so existing AGENT.md files keep working.
AGENT_TYPE_LLM = "llm"
AGENT_TYPE_SCRIPT = "script"
AGENT_TYPES = (AGENT_TYPE_LLM, AGENT_TYPE_SCRIPT)

# Defaults for the optional AGENT.md metadata schema.
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_ITERATIONS = 100
DEFAULT_MAX_OUTPUT_TOKENS = 4096

# Script agents run in descending priority bands; agents sharing a band run together.
DEFAULT_PRIORITY = 100
DEFAULT_SCRIPT_TIMEOUT = 120

# Where a script agent's automatic run sits relative to the orchestrator. Omitting
# `triggerPoint` means there is no automatic run at all — the agent is reached only by
# delegation.
TRIGGER_POINT_BEFORE = "before_orchestrator"
TRIGGER_POINT_AFTER = "after_orchestrator"
TRIGGER_POINTS = (TRIGGER_POINT_BEFORE, TRIGGER_POINT_AFTER)

# How a script agent was reached.
INVOCATION_TRIGGER = "trigger"
INVOCATION_DELEGATION = "delegation"

DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-opus-5"

DEFAULT_INSTRUCTIONS = (
    "You're an helpful assistant. Use any relevant tool at your disposal to "
    "answer the user query."
)

AGENT_TOOL_PREFIX = "agent__"

# Effort levels understood by the model layer. "none" disables the hint.
EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

_SLUG = re.compile(r"[^0-9A-Za-z_-]+")


def slugify(name: str) -> str:
    """Normalize an agent name into a legal tool-name fragment."""
    return _SLUG.sub("_", name).strip("_") or "agent"


@dataclass
class MCPServerConfig:
    """One entry of an agent's `mcp:` list.

    `enable` defaults to True: listing a server is taken as intent to use it, and
    `enable: false` is how you park an entry without deleting it.
    """

    name: str
    enable: bool = True
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass
class AgentConfig:
    """A validated agent loaded from `<agents>/<dir>/AGENT.md`.

    Two shapes share this type. An `llm` agent runs a model with tools. A `script` agent
    runs a Python `run()` function with no model at all.

    Both are offered to the orchestrator as delegation tools by default; a script agent
    opts out with `avoid_orchestrator: true`. A script agent runs on its own only if it
    names a `trigger_point` — before or after the orchestrator — and then whenever its
    `trigger_rule` matches, or on every message if it has none.
    """

    name: str
    description: str
    instructions: str
    path: Path
    type: str = AGENT_TYPE_LLM

    # llm agents only.
    provider: str = ""
    model: str = ""
    effort: str = DEFAULT_EFFORT
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    base_url: str = ""
    api_key: str = ""
    mcp: list[MCPServerConfig] = field(default_factory=list)

    # script agents only.
    script: str = ""
    priority: int = DEFAULT_PRIORITY
    send_output: bool = False
    timeout: int = DEFAULT_SCRIPT_TIMEOUT
    trigger_rule: TriggerRule | None = None
    trigger_point: str | None = None
    avoid_orchestrator: bool = False

    @property
    def is_script(self) -> bool:
        return self.type == AGENT_TYPE_SCRIPT

    @property
    def is_llm(self) -> bool:
        return self.type == AGENT_TYPE_LLM

    @property
    def delegatable(self) -> bool:
        """Whether the orchestrator is offered this agent as a tool.

        `avoid_orchestrator` is script-only metadata and stays False for `llm` agents,
        which are always delegatable — delegation is the only way to reach them.
        """
        return not self.avoid_orchestrator

    @property
    def runs_automatically(self) -> bool:
        """Whether this agent runs on its own, without the orchestrator asking.

        Only a `trigger_point` turns that on. Without one a script agent is reached solely
        by delegation, however its `trigger_rule` is written.
        """
        return self.is_script and self.trigger_point in TRIGGER_POINTS

    @property
    def runs_before_orchestrator(self) -> bool:
        return self.runs_automatically and self.trigger_point == TRIGGER_POINT_BEFORE

    @property
    def runs_after_orchestrator(self) -> bool:
        return self.runs_automatically and self.trigger_point == TRIGGER_POINT_AFTER

    @property
    def reachable(self) -> bool:
        """Whether anything can ever run this agent."""
        return self.delegatable or self.runs_automatically

    @property
    def tool_name(self) -> str:
        return f"{AGENT_TOOL_PREFIX}{slugify(self.name)}"

    @property
    def script_path(self) -> Path:
        return self.path / self.script

    @property
    def enabled_mcp_servers(self) -> list[MCPServerConfig]:
        """The servers to actually start for this agent."""
        return [server for server in self.mcp if server.enable]

    def triggered_by(self, values: dict[str, str | None]) -> bool:
        """Whether this script agent's automatic run should fire for a message.

        A script agent with no rule is unconditional *within its trigger point*; with no
        trigger point there is no automatic run for this to gate. An explicit delegation
        from the orchestrator ignores the rule either way, because the model asking for the
        agent by name is the decision the rule would otherwise make.
        """
        if self.trigger_rule is None:
            return True
        return self.trigger_rule.matches(values)


@dataclass
class ModelConfig:
    """Model settings for the orchestration loop itself."""

    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    base_url: str = ""
    api_key: str = ""


@dataclass
class ToolCall:
    """A single tool invocation requested by the model."""

    id: str
    name: str
    arguments: str = ""

    def parsed_arguments(self) -> dict[str, Any]:
        import json

        if not self.arguments.strip():
            return {}
        try:
            parsed = json.loads(self.arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


@dataclass
class Completion:
    """A normalized model response."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    cost: float = 0.0
    finish_reason: str | None = None

    def as_message(self) -> dict[str, Any]:
        """Render this completion as an assistant message for the next turn."""
        message: dict[str, Any] = {"role": "assistant"}
        if self.content:
            message["content"] = self.content
        elif not self.tool_calls:
            message["content"] = ""
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments or "{}"},
                }
                for call in self.tool_calls
            ]
        return message


@dataclass
class AgentResult:
    """The outcome of one delegated agent run."""

    agent: str
    task: str
    output: str = ""
    iterations: int = 0
    cost: float = 0.0
    error: str | None = None
    max_iterations_reached: bool = False

    def as_tool_content(self) -> str:
        if self.error:
            return f"[{self.agent} failed] {self.error}"
        if not self.output.strip():
            return f"[{self.agent}] completed with no output."
        if self.max_iterations_reached:
            return (
                f"{self.output}\n\n[{self.agent} stopped after reaching its "
                f"{self.iterations}-iteration limit; the result may be incomplete.]"
            )
        return self.output


@dataclass
class ScriptResult:
    """The outcome of one script agent run."""

    agent: str
    output: str = ""
    error: str | None = None
    priority: int = DEFAULT_PRIORITY
    sent_to_client: bool = False
    invocation: str = INVOCATION_TRIGGER
    trigger_point: str | None = None
    stop_execution: bool = False

    @property
    def succeeded(self) -> bool:
        return self.error is None

    def as_context(self) -> str:
        """How this result is described to the orchestrator.

        `send_output` decides whether the user has already seen the text, and the model
        needs to know which — otherwise it paraphrases back something already on screen.
        """
        if self.error:
            return f"### {self.agent} (failed)\n{self.error}"
        body = self.output.strip() or "(no output)"
        if self.sent_to_client:
            seen = "already shown to the user — do not repeat it, build on it"
        else:
            seen = "internal context — the user has not seen this"
        return f"### {self.agent} ({seen})\n{body}"

    def as_tool_content(self) -> str:
        """How this result is returned when the orchestrator delegated to the script.

        Mirrors `AgentResult.as_tool_content` so the model sees one shape whichever kind
        of agent it called.
        """
        if self.error:
            return f"[{self.agent} failed] {self.error}"
        body = self.output.strip()
        if not body:
            return f"[{self.agent}] completed with no output."
        if self.sent_to_client:
            return (
                f"{body}\n\n[This output was already posted to the user verbatim; "
                "do not repeat it.]"
            )
        return body


@dataclass
class RunResult:
    """The outcome of handling one user query."""

    output: str = ""
    iterations: int = 0
    cost: float = 0.0
    error: str | None = None
    max_iterations_reached: bool = False
    agent_results: list[AgentResult] = field(default_factory=list)
    script_results: list[ScriptResult] = field(default_factory=list)
    orchestrator_ran: bool = False
    stopped_by: str | None = None

    @property
    def stopped(self) -> bool:
        """Whether a script agent halted the run with `stop_execution`."""
        return self.stopped_by is not None
