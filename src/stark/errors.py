class StarkError(Exception):
    """Base class for every error raised by Stark."""


class AgentDiscoveryError(StarkError):
    """The agents directory is missing or cannot be read."""


class AgentValidationError(StarkError):
    """An AGENT.md file is missing mandatory metadata."""


class ListenerError(StarkError):
    """The requested listener is unknown or cannot be started."""


class MCPError(StarkError):
    """An MCP server could not be started or queried."""
