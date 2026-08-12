"""Running a `type: script` agent — a Python `run()` function, no model involved."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
from typing import Any, Callable

from ..errors import StarkError
from ..logger import get_logger
from ..types import AgentConfig, ScriptResult

logger = get_logger("script")

ENTRY_POINT = "run"
MAX_OUTPUT_CHARS = 20_000


class ScriptLoadError(StarkError):
    """A script agent's file could not be imported, or has no usable `run()`."""


def load_entry_point(agent: AgentConfig) -> Callable[..., Any]:
    """Import a script agent's module and return its `run` callable.

    Called once at startup rather than per message: a syntax error or a missing `run()`
    should stop the process at boot, not surface on the first message that triggers it.
    Import failures raise `ScriptLoadError`.
    """
    path = agent.script_path
    # Namespaced so two agents can both ship a `handler.py` without colliding in
    # sys.modules, and so a script never shadows a real package.
    module_name = f"stark_script_{agent.path.name}_{path.stem}".replace("-", "_")

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ScriptLoadError(f"{path}: cannot be imported")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:  # a script's import-time code can raise anything
        sys.modules.pop(module_name, None)
        raise ScriptLoadError(f"{path}: failed to import — {type(exc).__name__}: {exc}") from exc

    entry = getattr(module, ENTRY_POINT, None)
    if entry is None:
        raise ScriptLoadError(f"{path}: no `{ENTRY_POINT}()` function found")
    if not callable(entry):
        raise ScriptLoadError(f"{path}: `{ENTRY_POINT}` is not callable")

    return entry


def _as_text(value: Any) -> str:
    """Normalise whatever `run()` returned into text for the client and the model."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list, tuple)):
        try:
            text = json.dumps(value, indent=2, default=str)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)

    if len(text) > MAX_OUTPUT_CHARS:
        return f"{text[:MAX_OUTPUT_CHARS]}\n\n[truncated at {MAX_OUTPUT_CHARS} characters]"
    return text


class ScriptRunner:
    """Executes one script agent's `run()` against a message payload."""

    def __init__(self, agent: AgentConfig, entry_point: Callable[..., Any]):
        self.agent = agent
        self.entry_point = entry_point

    async def run(self, payload: dict[str, Any]) -> ScriptResult:
        """Call `run(payload)` and capture the outcome.

        Never raises: a failing script is reported as a `ScriptResult` with an error so
        the phase can carry on and the orchestrator still learns what happened.
        """
        result = ScriptResult(agent=self.agent.name, priority=self.agent.priority)

        try:
            value = await asyncio.wait_for(self._invoke(payload), timeout=self.agent.timeout)
        except asyncio.TimeoutError:
            result.error = f"timed out after {self.agent.timeout}s"
            logger.error("Script agent '%s' %s", self.agent.name, result.error)
            return result
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            logger.error("Script agent '%s' failed: %s", self.agent.name, result.error)
            return result

        result.output = _as_text(value)
        return result

    async def _invoke(self, payload: dict[str, Any]) -> Any:
        """Await an async `run()`, or run a sync one off the event loop.

        A synchronous `run()` doing blocking I/O would otherwise stall every MCP session
        and every other in-flight query, so it goes to a worker thread. Note the timeout
        can abandon that thread but cannot kill it — a truly wedged script leaks one
        thread for the life of the process.
        """
        if inspect.iscoroutinefunction(self.entry_point):
            return await self.entry_point(payload)

        value = await asyncio.to_thread(self.entry_point, payload)
        # A sync function may still hand back an awaitable.
        if inspect.isawaitable(value):
            return await value
        return value
