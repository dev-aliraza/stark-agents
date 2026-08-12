from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..env import expand_tree
from ..errors import AgentValidationError
from ..logger import get_logger
from ..triggers import TriggerRuleError
from ..triggers import parse as parse_trigger_rule
from ..types import (
    AGENT_TYPE_LLM,
    AGENT_TYPE_SCRIPT,
    AGENT_TYPES,
    DEFAULT_EFFORT,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_PRIORITY,
    DEFAULT_SCRIPT_TIMEOUT,
    EFFORT_LEVELS,
    AgentConfig,
    MCPServerConfig,
)
from . import frontmatter

logger = get_logger("parsers")

AGENT_FILE = "AGENT.md"

# Mandatory metadata depends on the agent type: a script agent has no model, and an llm
# agent has no script.
COMMON_REQUIRED_KEYS = ("name", "description")
LLM_REQUIRED_KEYS = COMMON_REQUIRED_KEYS + ("provider", "model")
SCRIPT_REQUIRED_KEYS = COMMON_REQUIRED_KEYS + ("script",)

# Kept for backwards compatibility with anything importing the old name.
REQUIRED_KEYS = LLM_REQUIRED_KEYS

# Keys that only mean something for one type; present on the other, they are ignored.
LLM_ONLY_KEYS = ("provider", "model", "effort", "max_iterations", "max_output_tokens", "mcp")
SCRIPT_ONLY_KEYS = ("script", "priority", "send_output", "triggerRule", "timeout")

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


def _optional_bool(metadata: dict[str, Any], key: str, source: Path) -> bool:
    value = metadata.get(key)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "yes", "1", "on"}:
        return True
    if text in {"false", "no", "0", "off", ""}:
        return False
    logger.warning("%s: '%s' is not a boolean; treating it as false", source, key)
    return False


def _parse_type(metadata: dict[str, Any], source: Path) -> str:
    raw = metadata.get("type")
    if raw is None:
        return AGENT_TYPE_LLM
    agent_type = str(raw).strip().lower()
    if agent_type not in AGENT_TYPES:
        raise AgentValidationError(
            f"{source}: unknown type '{raw}'; expected one of {', '.join(AGENT_TYPES)}"
        )
    return agent_type


def _parse_script(metadata: dict[str, Any], source: Path) -> str:
    """Validate the script filename and that it exists inside the agent directory.

    Checked at load time so a typo fails at startup rather than the first time a message
    would have triggered the agent.
    """
    script = _require_str(metadata, "script", source)

    candidate = (source.parent / script).resolve()
    root = source.parent.resolve()
    if root != candidate and root not in candidate.parents:
        raise AgentValidationError(
            f"{source}: script '{script}' is outside the agent directory"
        )
    if not candidate.is_file():
        raise AgentValidationError(f"{source}: script '{script}' does not exist")

    return script


def _parse_trigger_rule(metadata: dict[str, Any], source: Path):
    """Parse `triggerRule`, or return None when the agent runs unconditionally."""
    raw = metadata.get("triggerRule")
    if raw is None:
        return None
    try:
        return parse_trigger_rule(raw)
    except TriggerRuleError as exc:
        raise AgentValidationError(f"{source}: invalid triggerRule — {exc}") from exc


def _warn_on_irrelevant_keys(metadata: dict[str, Any], agent_type: str, source: Path) -> None:
    """Flag metadata that does nothing for this agent type.

    Silently ignoring these is how someone ends up convinced a triggerRule is broken when
    the agent is simply an llm agent that never had one.
    """
    ignored = SCRIPT_ONLY_KEYS if agent_type == AGENT_TYPE_LLM else LLM_ONLY_KEYS
    present = [key for key in ignored if metadata.get(key) is not None]
    if not present:
        return
    other = AGENT_TYPE_SCRIPT if agent_type == AGENT_TYPE_LLM else AGENT_TYPE_LLM
    logger.warning(
        "%s: %s only apply to '%s' agents and %s ignored on this '%s' agent",
        source,
        ", ".join(f"'{key}'" for key in present),
        other,
        "is" if len(present) == 1 else "are",
        agent_type,
    )


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
            f"{agent_file}: no YAML frontmatter found; expected keys "
            f"{', '.join(LLM_REQUIRED_KEYS)}"
        )

    metadata = expand_tree(metadata)

    agent_type = _parse_type(metadata, agent_file)
    required = SCRIPT_REQUIRED_KEYS if agent_type == AGENT_TYPE_SCRIPT else LLM_REQUIRED_KEYS

    missing = [key for key in required if not str(metadata.get(key) or "").strip()]
    if missing:
        raise AgentValidationError(
            f"{agent_file}: a '{agent_type}' agent is missing mandatory key(s) "
            f"{', '.join(missing)}"
        )

    _warn_on_irrelevant_keys(metadata, agent_type, agent_file)

    common = {
        "name": _require_str(metadata, "name", agent_file),
        "description": _require_str(metadata, "description", agent_file),
        "instructions": body,
        "path": agent_file.parent,
        "type": agent_type,
    }

    if agent_type == AGENT_TYPE_SCRIPT:
        return AgentConfig(
            **common,
            script=_parse_script(metadata, agent_file),
            priority=_optional_int(metadata, "priority", DEFAULT_PRIORITY, agent_file),
            send_output=_optional_bool(metadata, "send_output", agent_file),
            timeout=_optional_int(metadata, "timeout", DEFAULT_SCRIPT_TIMEOUT, agent_file),
            trigger_rule=_parse_trigger_rule(metadata, agent_file),
        )

    return AgentConfig(
        **common,
        provider=_require_str(metadata, "provider", agent_file).lower(),
        model=_require_str(metadata, "model", agent_file),
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
