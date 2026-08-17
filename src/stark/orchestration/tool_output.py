"""Unpacking whatever a tool returned.

A tool may return a plain string — nearly all do — or a `ToolResult` carrying text plus
images. Both the agent loop and the orchestrator need to take those apart the same way, and a
disagreement between them would show up as images silently vanishing in one path only.
"""

from __future__ import annotations

from typing import Any

from ..types import ToolImage, ToolResult


def split_result(result: Any) -> tuple[str, list[ToolImage]]:
    """Separate a tool's text from any images it produced.

    Anything that is not a `ToolResult` is stringified, which keeps MCP tools and every
    existing native toolset working untouched.
    """
    if isinstance(result, ToolResult):
        return result.text, list(result.images)
    return result if isinstance(result, str) else str(result), []
