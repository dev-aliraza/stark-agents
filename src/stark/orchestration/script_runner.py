"""Running a `type: script` agent — a Python `run()` function, no model involved."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
from typing import Any, Callable, Sequence

from ..errors import StarkError
from ..listeners.base import Message
from ..logger import get_logger
from ..types import (
    INVOCATION_DELEGATION,
    INVOCATION_TRIGGER,
    AgentConfig,
    ScriptResult,
)

logger = get_logger("script")

ENTRY_POINT = "run"
MAX_OUTPUT_CHARS = 20_000

# Reserved keys in a mapping returned by `run()`.
STOP_KEY = "stop_execution"
OUTPUT_KEY = "output"

_TRUE = {"true", "yes", "1", "on"}
_FALSE = {"false", "no", "0", "off", ""}


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


def build_payload(
    agent: AgentConfig,
    message: Message,
    *,
    invocation: str = INVOCATION_TRIGGER,
    prior: Sequence[ScriptResult] = (),
    task: str = "",
    context: str = "",
    orchestrator_output: str = "",
) -> dict[str, Any]:
    """What `run()` receives.

    A plain dict, not the Message dataclass, so a script never imports stark and can be
    unit-tested on its own. The same keys are present however the agent was reached, so
    one script can serve both a trigger and a delegation:

    * `text` is always the user's message, never the orchestrator's instruction.
    * `task` and `context` are set only when the orchestrator delegated; `invocation`
      says which happened.
    * `orchestrator_output` is the answer the orchestrator produced, so it is only
      populated for an `after_orchestrator` run.
    """
    return {
        "text": message.text,
        "user": message.user,
        "channel": message.channel,
        "thread": message.thread,
        "meta": message.meta,
        "agent": agent.name,
        "workspace": str(agent.path),
        "invocation": invocation,
        "task": task,
        "context": context,
        "orchestrator_output": orchestrator_output,
        "prior_outputs": [
            {"agent": item.agent, "output": item.output, "error": item.error}
            for item in prior
        ],
    }


def _as_stop_flag(raw: Any, agent: str) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    logger.warning(
        "Script agent '%s' returned %s=%r, which is not a boolean; treating it as false",
        agent,
        STOP_KEY,
        raw,
    )
    return False


def extract_stop(value: Any, agent: str) -> tuple[bool, Any]:
    """Split a `stop_execution` request out of whatever `run()` returned.

    A script asks to halt the run by returning a mapping with `stop_execution: true`. The
    flag is a control signal, not output, so it is removed before the rest is rendered:

    * `{"stop_execution": True}` → stop, with nothing to show.
    * `{"stop_execution": True, "output": "Ignored: duplicate"}` → stop, showing that text.
    * `{"stop_execution": True, "reason": "spam"}` → stop, showing `{"reason": "spam"}`.

    Any other return type is output and nothing else, so a script that never wants to stop
    the run needs to know nothing about this.
    """
    if not isinstance(value, dict) or STOP_KEY not in value:
        return False, value

    remaining = {key: item for key, item in value.items() if key != STOP_KEY}
    stop = _as_stop_flag(value[STOP_KEY], agent)

    if OUTPUT_KEY in remaining and len(remaining) == 1:
        return stop, remaining[OUTPUT_KEY]
    return stop, remaining or None


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
        invocation = str(payload.get("invocation") or INVOCATION_TRIGGER)
        if invocation not in (INVOCATION_TRIGGER, INVOCATION_DELEGATION):
            invocation = INVOCATION_TRIGGER
        result = ScriptResult(
            agent=self.agent.name,
            priority=self.agent.priority,
            invocation=invocation,
            trigger_point=self.agent.trigger_point,
        )

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

        result.stop_execution, output = extract_stop(value, self.agent.name)
        result.output = _as_text(output)
        if result.stop_execution:
            logger.info(
                "Script agent '%s' requested %s; nothing further will run for this message",
                self.agent.name,
                STOP_KEY,
            )
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
