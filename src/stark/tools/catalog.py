"""What native tools exist, what each one accepts, and how to load it.

Native tools are Stark's own capabilities, running in-process. An agent asks for one in its
`tools:` block; `mcp:` remains for third-party servers running as subprocesses.

This module is deliberately import-light. Validating an AGENT.md has to know every tool name
and every setting each accepts, and it must not pay for httpx to find that out
— so the specs live here as data and `load()` imports the implementation only when a tool is
actually built.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

FILE = "file"
SHELL = "shell"
WEBSEARCH = "websearch"
BROWSER = "browser"


@runtime_checkable
class ToolSet(Protocol):
    """One family of tools an agent can call.

    `FileTools` was the original shape and the rest follow it: a set of schemas, a way to
    say which names belong to you, and one entry point to run them. `aclose` exists because
    a toolset may hold a resource — a connection, a subprocess — and the registry
    that built it is responsible for shutting it down.

    `call` returns a `str` for almost every tool. A toolset that also produces something to
    look at may return a `ToolResult` instead, whose images are sent to the model as their
    own message. Nothing calling a toolset needs to know which it will get.

    One optional hook, not required here because only `browser` implements it:
    `needs_vision(tool_name) -> bool` marks tools that are pointless without a model that can
    see. `ToolBox` withholds those from a text-only model, and treats a toolset without the
    method as having no such tools.
    """

    def schemas(self) -> list[dict[str, Any]]: ...

    def owns(self, tool_name: str) -> bool: ...

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> Any: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class ToolSpec:
    """The declaration of one native tool, used for validation and construction."""

    name: str
    module: str
    factory: str
    settings: tuple[str, ...] = ()
    # Handed to every agent and to the orchestrator without being declared. `tools:` then
    # exists only to configure or switch it off.
    always_on: bool = False
    # Rooted in a directory: the agent's own folder, which is what makes it a sandbox.
    needs_root: bool = False
    # Extras named in the error when the import fails, so the fix is in the message.
    extras: tuple[str, ...] = ()
    summary: str = ""

    def load(self) -> Any:
        """Import and return the factory. Raises ImportError with a fixable message."""
        from importlib import import_module

        try:
            module = import_module(self.module)
        except ImportError as exc:
            if not self.extras:
                raise
            options = " or ".join(f"pip install 'stark-agents[{item}]'" for item in self.extras)
            raise ImportError(
                f"the '{self.name}' tool needs a dependency that is not installed "
                f"({exc}). Try: {options}"
            ) from exc
        return getattr(module, self.factory)


CATALOG: dict[str, ToolSpec] = {
    FILE: ToolSpec(
        name=FILE,
        module="stark.tools.file",
        factory="FileTools",
        settings=(),
        always_on=True,
        needs_root=True,
        summary="list, read, write, delete and run files in the agent's own directory",
    ),
    SHELL: ToolSpec(
        name=SHELL,
        module="stark.tools.shell",
        factory="ShellTools",
        settings=("allow", "cwd", "timeout"),
        summary="run shell commands",
    ),
    WEBSEARCH: ToolSpec(
        name=WEBSEARCH,
        module="stark.tools.websearch",
        factory="WebSearchTools",
        settings=("search_provider", "search_key", "allow_private"),
        extras=("websearch",),
        summary="search the web and read the pages it finds",
    ),
    BROWSER: ToolSpec(
        name=BROWSER,
        module="stark.tools.browser",
        factory="BrowserTools",
        settings=(
            "host", "port", "token", "timeout", "connect_timeout",
            "vision", "attach_debugger", "show_activity", "screenshot_path",
        ),
        summary="drive the user's own Chrome through the stark-browser extension",
    ),
}

TOOL_NAMES = tuple(CATALOG)
ALWAYS_ON = tuple(name for name, spec in CATALOG.items() if spec.always_on)


def spec_for(name: str) -> ToolSpec | None:
    return CATALOG.get((name or "").strip().lower())


def known_settings(name: str) -> tuple[str, ...]:
    spec = spec_for(name)
    return spec.settings if spec else ()


@dataclass
class ToolFilter:
    """`include`/`exclude` over individual tool names, shared by native and MCP tools."""

    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)

    def allows(self, tool_name: str) -> bool:
        if self.include and tool_name not in self.include:
            return False
        return tool_name not in self.exclude

    def apply(self, schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            schema for schema in schemas if self.allows(schema["function"]["name"])
        ]
