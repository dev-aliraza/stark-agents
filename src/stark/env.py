import os
import re
from typing import Any

# ${VAR} or ${VAR:-fallback}
_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand(value: str) -> str:
    """Substitute ${VAR} / ${VAR:-default} references from the environment.

    An unset variable with no default expands to an empty string, which keeps a
    partially configured AGENT.md loadable instead of crashing discovery.
    """

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        return os.environ.get(name) or (default or "")

    return _PATTERN.sub(replace, value)


def expand_tree(value: Any) -> Any:
    """Recursively expand environment references in strings, dicts and lists."""
    if isinstance(value, str):
        return expand(value)
    if isinstance(value, dict):
        return {key: expand_tree(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_tree(item) for item in value]
    return value
