from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from ..logger import get_logger

logger = get_logger("tools")

LIST = "file_list"
READ = "file_read"
RUN = "file_run"
WRITE = "file_write"
DELETE = "file_delete"

BUILTIN_TOOL_NAMES = (LIST, READ, RUN, WRITE, DELETE)

MAX_READ_CHARS = 40_000
MAX_OUTPUT_CHARS = 20_000
# Refused rather than truncated: silently cutting a file in half is data loss disguised
# as success.
MAX_WRITE_CHARS = 100_000
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 900

# An agent's own definition is configuration, not data. Overwriting it changes what the
# agent is on the next boot, and deleting it removes the agent entirely, so neither is
# something a model should be able to do while carrying out a task.
PROTECTED_NAMES = ("AGENT.md",)

_IGNORED = {"__pycache__", ".git", ".venv", "venv", "node_modules", ".DS_Store"}


def schemas() -> list[dict[str, Any]]:
    """Tool schemas for the file toolset given to every agent."""
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
                "name": WRITE,
                "description": (
                    "Create a UTF-8 text file in your own agent directory, or replace one "
                    "you already have. Use this to save a result, a report, or a script "
                    "you intend to run. Refuses to replace an existing file unless "
                    "'overwrite' is true, so read it first if you meant to change it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Path relative to your agent directory. Missing parent "
                                "folders are created."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": "The complete file contents.",
                        },
                        "overwrite": {
                            "type": "boolean",
                            "description": (
                                "Set true to replace the file if it already exists. "
                                "Defaults to false."
                            ),
                        },
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": DELETE,
                "description": (
                    "Delete a file from your own agent directory, or an empty folder. "
                    "This cannot be undone, so only delete something you created or were "
                    "told to remove. A folder with anything in it is refused."
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


class FileTools:
    """File and script access confined to a single agent's directory.

    Every path an agent supplies is resolved and checked against its own directory, so a
    delegated task cannot read, write, delete or execute anything elsewhere on the machine.

    Note what that does and does not bound. It confines the *paths* an agent may name, not
    the process it starts: a script reached through `file_run` is ordinary local code
    with the host user's permissions. Writing and deleting are offered on the same terms —
    a script could already do both — but an agent's own `AGENT.md` is off limits either
    way, since that is the definition of the agent rather than its data.
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

    def _resolve_mutable(self, raw_path: str, verb: str) -> Path:
        """Resolve a path an agent wants to change, refusing the ones it must not."""
        path = self._resolve(raw_path)
        if path == self.root:
            raise ValueError(f"cannot {verb} your agent directory itself")
        if path.name in PROTECTED_NAMES:
            raise ValueError(
                f"cannot {verb} '{path.name}' — it defines this agent. Ask whoever "
                "maintains the agent to change it."
            )
        return path

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        try:
            if tool_name == LIST:
                return self._list(str(arguments.get("pattern") or "*"))
            if tool_name == READ:
                return self._read(str(arguments.get("path") or ""))
            if tool_name == WRITE:
                return self._write(
                    str(arguments.get("path") or ""),
                    arguments.get("content"),
                    bool(arguments.get("overwrite")),
                )
            if tool_name == DELETE:
                return self._delete(str(arguments.get("path") or ""))
            if tool_name == RUN:
                return await self._run(
                    str(arguments.get("script") or ""),
                    [str(item) for item in (arguments.get("args") or [])],
                    arguments.get("timeout"),
                )
        except ValueError as exc:
            return f"[error] {exc}"
        except Exception as exc:
            logger.debug("File tool %s failed: %s", tool_name, exc)
            return f"[error] {tool_name} failed: {exc}"
        return f"[error] unknown file tool '{tool_name}'"

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

    def _write(self, raw_path: str, raw_content: Any, overwrite: bool) -> str:
        if not raw_path:
            return "[error] 'path' is required"
        if raw_content is None:
            # An absent `content` is far more likely a malformed call than an intent to
            # create an empty file, and truncating an existing file by accident is costly.
            return "[error] 'content' is required; pass an empty string to create an empty file"

        content = raw_content if isinstance(raw_content, str) else str(raw_content)
        if len(content) > MAX_WRITE_CHARS:
            return (
                f"[error] content is {len(content)} characters, over the "
                f"{MAX_WRITE_CHARS} limit; write less, or split it across files"
            )

        path = self._resolve_mutable(raw_path, "overwrite")
        if path.is_dir():
            return f"[error] {raw_path} is a folder, not a file"
        existed = path.exists()
        if existed and not overwrite:
            return (
                f"[error] {raw_path} already exists; read it first, then pass "
                "overwrite=true if you really mean to replace it"
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        logger.info("%s %s in %s", "Replaced" if existed else "Created", raw_path, self.root)
        return (
            f"{'Replaced' if existed else 'Created'} {raw_path} "
            f"({len(content)} characters, {content.count(chr(10)) + 1} line(s))."
        )

    def _delete(self, raw_path: str) -> str:
        if not raw_path:
            return "[error] 'path' is required"

        path = self._resolve_mutable(raw_path, "delete")
        if not path.exists():
            return f"[error] no such file: {raw_path}"

        if path.is_dir():
            # No recursive delete: wiping a tree is irreversible and too broad a thing to
            # infer from a task description.
            if any(path.iterdir()):
                return (
                    f"[error] {raw_path} is not empty; delete the files inside it first "
                    "if you really mean to remove it"
                )
            path.rmdir()
            logger.info("Deleted folder %s in %s", raw_path, self.root)
            return f"Deleted empty folder {raw_path}."

        path.unlink()
        logger.info("Deleted %s in %s", raw_path, self.root)
        return f"Deleted {raw_path}."

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
