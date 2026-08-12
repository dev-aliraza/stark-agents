from __future__ import annotations

import re
from typing import Any

import yaml

# Leading YAML frontmatter fenced by --- lines, followed by the body.
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)\Z", re.DOTALL)


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Split a Markdown document into (frontmatter mapping, body).

    Returns an empty mapping when the document has no frontmatter, so callers can
    treat "no metadata" and "invalid metadata" the same way. Raises `yaml.YAMLError`
    when the frontmatter is present but malformed.
    """
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text.strip()

    raw, body = match.group(1), match.group(2)
    loaded = yaml.safe_load(raw)
    if loaded is None:
        return {}, body.strip()
    if not isinstance(loaded, dict):
        raise yaml.YAMLError("frontmatter must be a mapping of key: value pairs")
    return loaded, body.strip()
