"""Stark's native tools: the capabilities it ships, running in-process.

`file` is handed to every agent and to the orchestrator without being asked for — it is
confined to one directory, which is what makes that safe. Everything else is declared per
agent in a `tools:` block, so a shell or web access is something an agent has because
someone decided it should.

Only `file` and the catalog are imported here. `shell`, `websearch` and `browser` load through
`catalog.ToolSpec.load()` when a toolset is actually built, so `import stark` stays free of
their dependencies.
"""

from .catalog import ALWAYS_ON, BROWSER, CATALOG, FILE, SHELL, TOOL_NAMES, WEBSEARCH, ToolFilter, ToolSet, ToolSpec, known_settings, spec_for
from .file import BUILTIN_TOOL_NAMES, FileTools, schemas as file_schemas

__all__ = [
    "FileTools",
    "file_schemas",
    "BUILTIN_TOOL_NAMES",
    "CATALOG",
    "TOOL_NAMES",
    "ALWAYS_ON",
    "FILE",
    "SHELL",
    "WEBSEARCH",
    "BROWSER",
    "ToolSet",
    "ToolSpec",
    "ToolFilter",
    "spec_for",
    "known_settings",
]
