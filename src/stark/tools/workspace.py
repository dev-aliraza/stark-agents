from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from ..logger import get_logger

logger = get_logger("tools")

LIST = "workspace_list"
READ = "workspace_read"
RUN = "workspace_run"

BUILTIN_TOOL_NAMES = (LIST, READ, RUN)

MAX_READ_CHARS = 40_000
MAX_OUTPUT_CHARS = 20_000
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 900

_IGNORED = {"__pycache__", ".git", ".venv", "venv", "node_modules", ".DS_Store"}


def schemas() -> list[dict[str, Any]]:
    """Tool schemas for the workspace toolset given to every agent."""
    return [
        {
            "type": "function",
            "function": {
                "name": LIST,
                "description": (
                    "List the files in your own agent directory. Use this to discover "
                    "the scripts and data files available to you."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Optional glob pattern, e.g. '*.py'. Defaults to '*'.",
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": READ,
                "description": (
                    "Read a UTF-8 text file from your own agent directory, so you can "
                    "inspect a script or reference file before using it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to your agent directory.",
                        }
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": RUN,
                "description": (
                    "Run a script that lives in your own agent directory and return its "
                    "output. Python files (.py) run on the current interpreter; other "
                    "executables run directly. Use this when your instructions tell you "
                    "to run one of your scripts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "script": {
                            "type": "string",
                            "description": "Script filename relative to your agent directory.",
                        },
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional command-line arguments.",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": f"Seconds before the script is killed (default {DEFAULT_TIMEOUT}).",
                        },
                    },
                    "required": ["script"],
                },
            },
        },
    ]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n[truncated at {limit} characters]"


class WorkspaceTools:
    """File and script access confined to a single agent's directory.

    Every path an agent supplies is resolved and checked against its own directory,
    so a delegated task cannot read or execute anything elsewhere on the machine.
    """

    def __init__(self, root: Path):
        self.root = root.resolve()

    def owns(self, tool_name: str) -> bool:
        return tool_name in BUILTIN_TOOL_NAMES

    def _resolve(self, raw_path: str) -> Path:
        candidate = (self.root / raw_path).resolve() if not Path(raw_path).is_absolute() else Path(raw_path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(
                f"'{raw_path}' is outside your agent directory; only paths inside it are allowed"
            )
        return candidate

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        try:
            if tool_name == LIST:
                return self._list(str(arguments.get("pattern") or "*"))
            if tool_name == READ:
                return self._read(str(arguments.get("path") or ""))
            if tool_name == RUN:
                return await self._run(
                    str(arguments.get("script") or ""),
                    [str(item) for item in (arguments.get("args") or [])],
                    arguments.get("timeout"),
                )
        except ValueError as exc:
            return f"[error] {exc}"
        except Exception as exc:
            logger.debug("Workspace tool %s failed: %s", tool_name, exc)
            return f"[error] {tool_name} failed: {exc}"
        return f"[error] unknown workspace tool '{tool_name}'"

    def _list(self, pattern: str) -> str:
        entries = sorted(
            path
            for path in self.root.glob(pattern)
            if not any(part in _IGNORED for part in path.relative_to(self.root).parts)
        )
        if not entries:
            return f"No files matching '{pattern}' in your agent directory."

        lines = [f"Files in your agent directory (pattern '{pattern}'):"]
        for path in entries:
            relative = path.relative_to(self.root)
            if path.is_dir():
                lines.append(f"  {relative}/")
            else:
                lines.append(f"  {relative} ({path.stat().st_size} bytes)")
        return "\n".join(lines)

    def _read(self, raw_path: str) -> str:
        if not raw_path:
            return "[error] 'path' is required"
        path = self._resolve(raw_path)
        if not path.is_file():
            return f"[error] no such file: {raw_path}"
        return _truncate(path.read_text(encoding="utf-8", errors="replace"), MAX_READ_CHARS)

    async def _run(self, raw_script: str, args: list[str], raw_timeout: Any) -> str:
        if not raw_script:
            return "[error] 'script' is required"

        script = self._resolve(raw_script)
        if not script.is_file():
            return f"[error] no such script: {raw_script}"

        try:
            timeout = int(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        timeout = max(1, min(timeout, MAX_TIMEOUT))

        if script.suffix == ".py":
            argv = [sys.executable, str(script), *args]
        elif os.access(script, os.X_OK):
            argv = [str(script), *args]
        else:
            return (
                f"[error] {raw_script} is neither a .py file nor executable; "
                "make it executable or give it a .py extension"
            )

        logger.info("Running %s in %s", raw_script, self.root)
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return f"[error] {raw_script} timed out after {timeout}s"

        sections = [f"exit code: {process.returncode}"]
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        if out:
            sections.append(f"stdout:\n{_truncate(out, MAX_OUTPUT_CHARS)}")
        if err:
            sections.append(f"stderr:\n{_truncate(err, MAX_OUTPUT_CHARS)}")
        if not out and not err:
            sections.append("(no output)")
        return "\n\n".join(sections)
