from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ..errors import AgentDiscoveryError, AgentValidationError
from ..logger import get_logger
from ..types import AgentConfig
from .agent_md import AGENT_FILE, parse_agent_file

logger = get_logger("discovery")

_IGNORED_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv"}


def discover_agents(
    agents_dir: str | Path,
    exclude_agents: Iterable[str] | None = None,
) -> list[AgentConfig]:
    """Scan `agents_dir` and return every valid agent, sorted by directory name.

    Discovery is forgiving on purpose — one bad agent should not stop the process:

    * a directory without `AGENT.md` at its root is skipped silently
    * a directory named in `exclude_agents` is skipped
    * an `AGENT.md` missing mandatory metadata raises a warning and is skipped

    Only a missing or unreadable `agents_dir` is fatal.
    """
    root = Path(agents_dir).expanduser()
    excluded = {name.strip() for name in (exclude_agents or []) if name and name.strip()}

    if not root.exists():
        raise AgentDiscoveryError(
            f"agents directory not found: {root}. Create it, or pass agents=<path> to stark.run()."
        )
    if not root.is_dir():
        raise AgentDiscoveryError(f"agents path is not a directory: {root}")

    agents: list[AgentConfig] = []
    seen: dict[str, Path] = {}

    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name in _IGNORED_DIRS:
            continue

        if entry.name in excluded:
            logger.info("Skipping excluded agent directory: %s", entry.name)
            continue

        agent_file = entry / AGENT_FILE
        if not agent_file.is_file():
            logger.debug("Skipping %s: no %s at its root", entry.name, AGENT_FILE)
            continue

        try:
            config = parse_agent_file(agent_file)
        except AgentValidationError as exc:
            logger.warning("Skipping agent in %s: %s", entry.name, exc)
            continue

        if config.name in seen:
            logger.warning(
                "Skipping agent in %s: name '%s' is already used by %s",
                entry.name,
                config.name,
                seen[config.name],
            )
            continue

        seen[config.name] = entry
        agents.append(config)
        if config.is_script:
            logger.info(
                "Loaded script agent '%s' (script=%s, priority=%s)",
                config.name,
                config.script,
                config.priority,
            )
        else:
            enabled = config.enabled_mcp_servers
            logger.info(
                "Loaded agent '%s' (%s/%s, effort=%s, mcp=%s)",
                config.name,
                config.provider,
                config.model,
                config.effort,
                ", ".join(server.name for server in enabled) if enabled else "none",
            )

    if not agents:
        logger.warning(
            "No valid agents found in %s. The orchestrator will run without delegation tools.",
            root,
        )

    return agents
