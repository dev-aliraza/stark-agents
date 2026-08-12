from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Defaults for the optional AGENT.md metadata schema.
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_ITERATIONS = 100
DEFAULT_MAX_OUTPUT_TOKENS = 4096

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
    """A validated agent loaded from `<agents>/<dir>/AGENT.md`."""

    name: str
    description: str
    provider: str
    model: str
    instructions: str
    path: Path
    effort: str = DEFAULT_EFFORT
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    base_url: str = ""
    api_key: str = ""
    mcp: list[MCPServerConfig] = field(default_factory=list)

    @property
    def tool_name(self) -> str:
        return f"{AGENT_TOOL_PREFIX}{slugify(self.name)}"

    @property
    def enabled_mcp_servers(self) -> list[MCPServerConfig]:
        """The servers to actually start for this agent."""
        return [server for server in self.mcp if server.enable]


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
class RunResult:
    """The outcome of handling one user query."""

    output: str = ""
    iterations: int = 0
    cost: float = 0.0
    error: str | None = None
    max_iterations_reached: bool = False
    agent_results: list[AgentResult] = field(default_factory=list)
