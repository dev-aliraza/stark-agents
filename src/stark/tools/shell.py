"""Running shell commands, as a native toolset an agent asks for in its `tools:` block.

Be clear about what this is. It runs commands through the shell, as the user the process runs
as, with that user's permissions. It is **not a sandbox.** The guards below bound how long a
command runs, how much output comes back, and catch a few catastrophic typos. They do not
contain a determined command, and nothing here should be read as if they do.

Two things provide real containment, and both are the operator's to set:

    tools:
      shell:
        allow: [git, ls, rg]     # only these programs, one plain command per call
        cwd: ${REPO_PATH:-.}

and not listing `shell` at all for an agent that has no business running commands. Unlike
`file`, this tool is never handed out by default.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..logger import get_logger

logger = get_logger("shell")

RUN = "shell_run"
WHICH = "shell_which"
POLICY = "shell_policy"

SHELL_TOOL_NAMES = (RUN, WHICH, POLICY)

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 900
MAX_OUTPUT_CHARS = 20_000

# Refused when an allowlist is in force. Without one they are all legal, because piping and
# redirection are most of why a shell is useful.
SHELL_METACHARACTERS = (";", "&", "|", "`", "$(", "${", ">", "<", "\n", "\r")

# These catch a mistake, not an attacker: a model that has misread its instructions, or a
# generated command with an empty variable in it. Anyone who wants past this list can walk
# past it, which is why the list is short and the module docstring says what it is worth.
REFUSED_PATTERNS = (
    (re.compile(r"\bmkfs(\.\w+)?\b"), "formatting a filesystem"),
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/(disk|sd|nvme|hd)"), "writing to a raw device"),
    (re.compile(r":\s*\(\s*\)\s*\{.*\}\s*;\s*:"), "a fork bomb"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "shutting the machine down"),
    (re.compile(r"\bchmod\s+(-[a-zA-Z]*\s+)*777\s+/(\s|$)"), "chmod 777 /"),
    (re.compile(r"\b(curl|wget)\b[^\n]*\|\s*(ba|z|k)?sh\b"), "piping a download into a shell"),
)

# Targets that make a recursive delete a catastrophe rather than a cleanup.
_RECKLESS_TARGETS = frozenset({"/", "/*", "~", "~/", "~/*", "/.", "$HOME", "${HOME}"})
_LONG_RECURSIVE = frozenset({"--recursive", "--dir", "-r", "-R"})


class ShellError(Exception):
    """The command was refused before it ran, with a reason worth showing the model."""


def _is_reckless_rm(command: str) -> bool:
    """Whether a command recursively deletes a root or a whole home directory.

    Tokenised rather than pattern-matched: `rm -rf /`, `rm -fr /`, `rm --recursive --force /`
    and `rm -r -f ~` are the same mistake written four ways, and a regex that catches all of
    them either misses one or catches `rm -rf ./build` too.
    """
    tokens = command.split()
    if not tokens or tokens[0].rsplit("/", 1)[-1] != "rm":
        return False

    recursive = False
    targets: list[str] = []
    for token in tokens[1:]:
        if token in _LONG_RECURSIVE:
            recursive = True
        elif token.startswith("--"):
            continue
        elif token.startswith("-"):
            recursive = recursive or "r" in token[1:] or "R" in token[1:]
        else:
            targets.append(token)

    return recursive and any(target in _RECKLESS_TARGETS for target in targets)


def normalise_allow(value: Any) -> tuple[str, ...]:
    """Accept a YAML list, or a comma-separated string for people who write one."""
    if value is None:
        return ()
    if isinstance(value, str):
        items: Iterable[str] = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        return ()
    return tuple(item.strip() for item in items if str(item).strip())


def check_command(command: str, allow: tuple[str, ...] = ()) -> str:
    """Validate a command before running it. Returns it stripped."""
    command = (command or "").strip()
    if not command:
        raise ShellError("'command' is required")

    refusal = "recursively deleting a root or home directory" if _is_reckless_rm(command) else ""
    for pattern, description in REFUSED_PATTERNS:
        if pattern.search(command):
            refusal = refusal or description
            break
    if refusal:
        raise ShellError(
            f"refused: this looks like {refusal}. If that is genuinely what you mean, a "
            "person should run it themselves."
        )

    if not allow:
        return command

    found = [character for character in SHELL_METACHARACTERS if character in command]
    if found:
        raise ShellError(
            "refused: an allowlist is in force, so only a single plain command is allowed "
            f"and {', '.join(repr(item) for item in found)} is not. Run one command per call."
        )

    program = command.split()[0]
    # A path is not the same program as its basename, so compare what was actually written.
    if program not in allow and Path(program).name not in allow:
        raise ShellError(
            f"refused: '{program}' is not in the allowed list ({', '.join(allow)}). Ask "
            "whoever maintains this agent to add it."
        )
    return command


def which(program: str) -> str | None:
    """Where a program would be found, or None. Cheaper than a failed run."""
    program = (program or "").strip()
    if not program or any(character in program for character in SHELL_METACHARACTERS):
        return None
    return shutil.which(program)


def _truncate(text: str) -> tuple[str, bool]:
    text = text.strip()
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return f"{text[:MAX_OUTPUT_CHARS]}\n\n[truncated at {MAX_OUTPUT_CHARS} characters]", True


@dataclass
class ShellResult:
    command: str
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False
    truncated: bool = False

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "command": self.command,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration, 3),
        }
        if self.stdout:
            payload["stdout"] = self.stdout
        if self.stderr:
            payload["stderr"] = self.stderr
        if not self.stdout and not self.stderr:
            payload["note"] = "the command produced no output"
        if self.timed_out:
            payload["timed_out"] = True
        if self.truncated:
            payload["truncated"] = True
        return payload


def schemas() -> list[dict[str, Any]]:
    """Tool schemas for the shell toolset."""
    return [
        {
            "type": "function",
            "function": {
                "name": RUN,
                "description": (
                    "Run a shell command and return its exit code, stdout and stderr. "
                    "Pipes, redirection and globs work. Commands run with no terminal and "
                    "no stdin, so anything that would prompt fails instead of hanging — "
                    "pass every answer as a flag. Check the exit code, not just the "
                    "output: a non-zero code with empty stderr is still a failure."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The command line to run.",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": (
                                f"Seconds before it is killed (default {DEFAULT_TIMEOUT}, "
                                f"max {MAX_TIMEOUT})."
                            ),
                        },
                        "cwd": {
                            "type": "string",
                            "description": (
                                "Directory to run in, relative to this tool's root. "
                                "Defaults to the root."
                            ),
                        },
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": WHICH,
                "description": (
                    "Check whether a program is installed, and where. Cheaper than "
                    "discovering it is missing through a failed command."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "program": {
                            "type": "string",
                            "description": "A program name, such as 'git'.",
                        }
                    },
                    "required": ["program"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": POLICY,
                "description": (
                    "Report what this tool will and will not run. Worth calling before "
                    "assuming a command is available: when an allowlist is in force, only "
                    "those programs run and only one command per call, so pipes are "
                    "refused."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


class ShellTools:
    """The shell toolset for one agent, configured from its `tools: shell:` block.

    One instance per agent, so two agents can have different allowlists and different
    working directories without knowing about each other.
    """

    def __init__(self, root: Path | str | None = None, settings: dict[str, Any] | None = None):
        settings = settings or {}
        self.allow = normalise_allow(settings.get("allow"))
        self.default_timeout = self._timeout(settings.get("timeout"), DEFAULT_TIMEOUT)

        base = Path(root or os.getcwd())
        configured = settings.get("cwd")
        self.root = (base / str(configured)).resolve() if configured else Path(base).resolve()

    # --- ToolSet ----------------------------------------------------------------------

    def schemas(self) -> list[dict[str, Any]]:
        return schemas()

    def owns(self, tool_name: str) -> bool:
        return tool_name in SHELL_TOOL_NAMES

    async def aclose(self) -> None:
        """Nothing to release: each command is its own subprocess, awaited or killed."""

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        try:
            if tool_name == RUN:
                result = await self.run(
                    str(arguments.get("command") or ""),
                    timeout=arguments.get("timeout"),
                    cwd=str(arguments.get("cwd") or "") or None,
                )
                return _as_json(result.as_payload())
            if tool_name == WHICH:
                program = str(arguments.get("program") or "")
                path = which(program)
                return _as_json({"program": program, "found": path is not None, "path": path})
            if tool_name == POLICY:
                return _as_json(self.policy())
        except ShellError as exc:
            return f"[error] {exc}"
        except Exception as exc:  # pragma: no cover - unexpected spawn failure
            logger.debug("Shell tool %s failed: %s", tool_name, exc)
            return f"[error] {tool_name} failed: {exc}"
        return f"[error] unknown shell tool '{tool_name}'"

    # --- the work ----------------------------------------------------------------------

    def policy(self) -> dict[str, Any]:
        return {
            "allowed_programs": list(self.allow) or None,
            "restricted": bool(self.allow),
            "single_command_only": bool(self.allow),
            "working_directory": str(self.root),
            "default_timeout_seconds": self.default_timeout,
            "max_timeout_seconds": MAX_TIMEOUT,
            "note": (
                "No allowlist is configured, so any command may run."
                if not self.allow
                else "Only the listed programs may run, one command per call."
            ),
        }

    @staticmethod
    def _timeout(value: Any, default: int) -> int:
        if value in (None, "", 0):
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return max(1, min(parsed, MAX_TIMEOUT))

    def _directory(self, cwd: str | None) -> Path:
        target = self.root if not cwd else (self.root / Path(cwd).expanduser()).resolve()
        if not target.is_dir():
            raise ShellError(f"'{cwd or target}' is not a directory")
        return target

    async def run(self, command: str, timeout=None, cwd: str | None = None) -> ShellResult:
        """Run one command and capture its outcome. Raises `ShellError` only if refused."""
        command = check_command(command, self.allow)
        directory = self._directory(cwd)
        seconds = self._timeout(timeout, self.default_timeout)

        started = time.monotonic()
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Without this an interactive command — `git commit` with no -m, anything that
            # prompts — blocks until the timeout instead of failing immediately.
            stdin=asyncio.subprocess.DEVNULL,
            cwd=str(directory),
            # Its own process group, so a timeout can kill the whole tree. Killing just the
            # shell would orphan whatever it started.
            start_new_session=True,
        )

        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=seconds)
        except asyncio.TimeoutError:
            timed_out = True
            _terminate_group(process)
            stdout, stderr = b"", b""
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:  # pragma: no cover - the kill did not take
                pass

        out, out_cut = _truncate(stdout.decode("utf-8", errors="replace"))
        err, err_cut = _truncate(stderr.decode("utf-8", errors="replace"))
        if timed_out and not err:
            err = f"timed out after {seconds}s and was killed"

        logger.info("Ran %r in %s (exit %s)", command, directory, process.returncode)
        return ShellResult(
            command=command,
            exit_code=process.returncode,
            stdout=out,
            stderr=err,
            duration=time.monotonic() - started,
            timed_out=timed_out,
            truncated=out_cut or err_cut,
        )


def _terminate_group(process) -> None:
    """SIGKILL the command's whole process group, falling back to the process itself."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except ProcessLookupError:  # pragma: no cover - already gone
            pass


def _as_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, default=str)
