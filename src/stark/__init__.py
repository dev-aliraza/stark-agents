"""Stark — a lightweight Python ADK for orchestrating multi-agent workflows.

Point `stark.run()` at a directory of Markdown-defined agents and it discovers them,
starts their MCP servers, and serves queries through a CLI or Slack listener:

    import stark

    stark.run(agents="./agents", listener="cli")
"""

from .listeners import Listener, Message, ResponseSink
from .logger import configure_logging, logger
from .orchestration import AgentRunner, Orchestrator, Registry
from .parsers import discover_agents, parse_agent_file
from .runtime import orchestrator_model, run, run_async
from .types import (
    DEFAULT_INSTRUCTIONS,
    AgentConfig,
    AgentResult,
    MCPServerConfig,
    ModelConfig,
    RunResult,
)

__all__ = [
    "run",
    "run_async",
    "orchestrator_model",
    "DEFAULT_INSTRUCTIONS",
    "AgentConfig",
    "AgentResult",
    "ModelConfig",
    "MCPServerConfig",
    "RunResult",
    "Registry",
    "Orchestrator",
    "AgentRunner",
    "Listener",
    "Message",
    "ResponseSink",
    "discover_agents",
    "parse_agent_file",
    "configure_logging",
    "logger",
]
