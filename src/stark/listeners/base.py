from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..types import RunResult


@dataclass
class Message:
    """One inbound user query, normalized across listeners."""

    text: str
    user: str | None = None
    channel: str | None = None
    thread: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class ResponseSink(ABC):
    """Where a run's output goes — the listener decides how to render it.

    The orchestrator streams text through `chunk`, reports progress through `event`,
    and closes with exactly one `final` or `error`.
    """

    async def status(self, text: str) -> None:
        """Announce that work has started."""

    @abstractmethod
    async def chunk(self, text: str) -> None:
        """Handle one incremental slice of the answer."""

    async def event(self, kind: str, detail: str, key: str | None = None) -> None:
        """Report progress: a tool call, or an agent starting or finishing.

        `kind` is one of `agent_start`, `agent_end`, `agent_error`, `tool`, `tool_end`.

        `key` correlates the start and end of the same unit of work, so a listener can
        update the line it already rendered instead of appending a second one. Agent
        names and tool names are not unique — the same agent can be delegated to twice
        in one turn — so matching on `detail` is not reliable.
        """

    @abstractmethod
    async def final(self, text: str) -> None:
        """Deliver the completed answer."""

    @abstractmethod
    async def error(self, text: str) -> None:
        """Report that the run failed."""


# A listener hands each inbound message to this callable along with a sink.
Handler = Callable[[Message, ResponseSink], Awaitable[RunResult]]


class Listener(ABC):
    """A source of user queries that keeps the process alive while it waits."""

    name = "listener"

    def __init__(self, handler: Handler):
        self.handler = handler

    @abstractmethod
    async def start(self) -> None:
        """Block, dispatching inbound messages until stopped."""

    async def stop(self) -> None:
        """Release any resources held by the listener."""
