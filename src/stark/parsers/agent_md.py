from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..env import expand_tree
from ..errors import AgentValidationError
from ..logger import get_logger
from ..types import (
    DEFAULT_EFFORT,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    EFFORT_LEVELS,
    AgentConfig,
    MCPServerConfig,
)
from . import frontmatter

logger = get_logger("parsers")

AGENT_FILE = "AGENT.md"

REQUIRED_KEYS = ("name", "description", "provider", "model")

_STDIO = "stdio"
_HTTP = "streamable_http"
_TRANSPORTS = (_STDIO, _HTTP)


def _require_str(metadata: dict[str, Any], key: str, source: Path) -> str:
    value = metadata.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise AgentValidationError(f"{source}: missing mandatory key '{key}'")
    if not isinstance(value, (str, int, float)):
        raise AgentValidationError(f"{source}: key '{key}' must be a string")
    return str(value).strip()


def _optional_int(metadata: dict[str, Any], key: str, default: int, source: Path) -> int:
    if key not in metadata or metadata[key] is None:
        return default
    try:
        parsed = int(metadata[key])
    except (TypeError, ValueError):
        logger.warning("%s: '%s' is not an integer; using default %s", source, key, default)
        return default
    if parsed <= 0:
        logger.warning("%s: '%s' must be positive; using default %s", source, key, default)
        return default
    return parsed


def _optional_str(metadata: dict[str, Any], key: str, default: str = "") -> str:
    value = metadata.get(key)
    if value is None:
        return default
    return str(value).strip()


def _parse_effort(metadata: dict[str, Any], source: Path) -> str:
    effort = _optional_str(metadata, "effort", DEFAULT_EFFORT) or DEFAULT_EFFORT
    normalized = effort.lower()
    if normalized not in EFFORT_LEVELS:
        logger.warning(
            "%s: unknown effort '%s' (expected one of %s); using '%s'",
            source,
            effort,
            ", ".join(EFFORT_LEVELS),
            DEFAULT_EFFORT,
        )
        return DEFAULT_EFFORT
    return normalized


def _str_list(raw: dict[str, Any], key: str, name: str, source: Path) -> list[str]:
    value = raw.get(key) or []
    if not isinstance(value, list):
        logger.warning("%s: mcp server '%s' %s must be a list; ignoring", source, name, key)
        return []
    return [str(item) for item in value]


def _str_map(raw: dict[str, Any], key: str, name: str, source: Path) -> dict[str, str]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        logger.warning("%s: mcp server '%s' %s must be a mapping; ignoring", source, name, key)
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _parse_mcp_server(raw: Any, index: int, source: Path) -> MCPServerConfig | None:
    """Parse one entry of the `mcp:` list, or return None if it is unusable."""
    if not isinstance(raw, dict):
        logger.warning("%s: mcp entry %d must be a mapping; skipping", source, index)
        return None

    name = str(raw.get("name") or "").strip()
    if not name:
        logger.warning("%s: mcp entry %d needs a 'name'; skipping", source, index)
        return None

    transport = str(raw.get("transport") or _STDIO).strip().lower()
    if transport not in _TRANSPORTS:
        logger.warning(
            "%s: mcp server '%s' has unsupported transport '%s' (expected %s); skipping",
            source,
            name,
            transport,
            " or ".join(_TRANSPORTS),
        )
        return None

    command = str(raw.get("command") or "").strip()
    url = str(raw.get("url") or "").strip()

    if transport == _STDIO and not command:
        logger.warning("%s: mcp server '%s' needs a 'command'; skipping", source, name)
        return None
    if transport == _HTTP and not url:
        logger.warning("%s: mcp server '%s' needs a 'url'; skipping", source, name)
        return None

    return MCPServerConfig(
        name=name,
        enable=bool(raw.get("enable", True)),
        transport=transport,
        command=command or None,
        args=_str_list(raw, "args", name, source),
        env=_str_map(raw, "env", name, source),
        url=url or None,
        headers=_str_map(raw, "headers", name, source),
        include=_str_list(raw, "include", name, source),
        exclude=_str_list(raw, "exclude", name, source),
    )


def _parse_mcp(metadata: dict[str, Any], source: Path) -> list[MCPServerConfig]:
    """Parse the optional `mcp:` list.

    Each entry is one server carrying its own `enable` flag; only enabled entries are
    ever started. An omitted `mcp` key means the agent has no MCP servers. Duplicate
    names are dropped so tool routing stays unambiguous.
    """
    raw = metadata.get("mcp")
    if raw is None:
        return []

    if isinstance(raw, dict):
        logger.warning(
            "%s: 'mcp' must be a list of servers, for example:\n"
            "  mcp:\n"
            "    - name: slack\n"
            "      enable: true\n"
            "      command: uvx\n"
            "      args: [\"mcp-slack\"]\n"
            "Found a mapping instead; no MCP servers loaded for this agent.",
            source,
        )
        return []

    if not isinstance(raw, list):
        logger.warning("%s: 'mcp' must be a list of servers; ignoring", source)
        return []

    servers: list[MCPServerConfig] = []
    seen: set[str] = set()

    for index, entry in enumerate(raw):
        server = _parse_mcp_server(entry, index, source)
        if server is None:
            continue
        if server.name in seen:
            logger.warning(
                "%s: mcp server '%s' is declared more than once; keeping the first",
                source,
                server.name,
            )
            continue
        seen.add(server.name)
        servers.append(server)

    return servers


def parse_agent_file(agent_file: Path) -> AgentConfig:
    """Load and validate a single AGENT.md file.

    Raises `AgentValidationError` when a mandatory key is missing or the
    frontmatter cannot be parsed.
    """
    try:
        text = agent_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentValidationError(f"{agent_file}: cannot be read ({exc})") from exc

    try:
        metadata, body = frontmatter.parse(text)
    except yaml.YAMLError as exc:
        raise AgentValidationError(f"{agent_file}: invalid YAML frontmatter ({exc})") from exc

    if not metadata:
        raise AgentValidationError(
            f"{agent_file}: no YAML frontmatter found; expected keys {', '.join(REQUIRED_KEYS)}"
        )

    metadata = expand_tree(metadata)

    missing = [key for key in REQUIRED_KEYS if not str(metadata.get(key) or "").strip()]
    if missing:
        raise AgentValidationError(
            f"{agent_file}: missing mandatory key(s) {', '.join(missing)}"
        )

    return AgentConfig(
        name=_require_str(metadata, "name", agent_file),
        description=_require_str(metadata, "description", agent_file),
        provider=_require_str(metadata, "provider", agent_file).lower(),
        model=_require_str(metadata, "model", agent_file),
        instructions=body,
        path=agent_file.parent,
        effort=_parse_effort(metadata, agent_file),
        max_iterations=_optional_int(
            metadata, "max_iterations", DEFAULT_MAX_ITERATIONS, agent_file
        ),
        max_output_tokens=_optional_int(
            metadata, "max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS, agent_file
        ),
        base_url=_optional_str(metadata, "base_url"),
        api_key=_optional_str(metadata, "api_key"),
        mcp=_parse_mcp(metadata, agent_file),
    )
