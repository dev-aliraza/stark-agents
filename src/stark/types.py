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

    Two shapes share this type. An `llm` agent runs a model with tools and is offered to
    the orchestrator. A `script` agent runs a Python `run()` function with no model at
    all, is never offered to the orchestrator, and is reached only by its `trigger_rule`
    (or unconditionally, when it has none).
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

    @property
    def is_script(self) -> bool:
        return self.type == AGENT_TYPE_SCRIPT

    @property
    def is_llm(self) -> bool:
        return self.type == AGENT_TYPE_LLM

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
        """Whether this script agent should run for a message.

        A script agent with no rule is unconditional: it has no other way to be reached,
        since script agents are never exposed to the orchestrator.
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
