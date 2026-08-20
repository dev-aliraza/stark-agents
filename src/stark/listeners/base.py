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


def trigger_values(message: Message) -> dict[str, str | None]:
    """The message fields a trigger expression may read.

    Defined here, beside `Message`, because two layers evaluate the same expressions
    against the same fields: a Slack listener filter deciding whether to respond at all,
    and a script agent's `triggerRule` deciding whether it runs. They must agree on what
    `text` means, so there is one mapping rather than one each.
    """
    return {
        "text": message.text,
        "user": message.user,
        "channel": message.channel,
        "thread": message.thread,
    }


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

    async def message(self, text: str) -> None:
        """Deliver a standalone message now, ahead of the final answer.

        Used by script agents with `send_output: true`. The default forwards to `chunk`
        so a custom sink keeps working; the CLI and Slack listeners override it to render
        a distinct block or post a separate message.
        """
        await self.chunk(f"{text}\n")

    async def event(self, kind: str, detail: str, key: str | None = None) -> None:
        """Report progress: a tool call, or an agent starting or finishing.

        `kind` is one of `agent_start`, `agent_end`, `agent_error`, `tool`, `tool_end`.

        `key` correlates the start and end of the same unit of work, so a listener can
        update the line it already rendered instead of appending a second one. Agent
        names and tool names are not unique — the same agent can be delegated to twice
        in one turn — so matching on `detail` is not reliable.
        """

    async def detail(self, kind: str, text: str, key: str | None = None) -> None:
        """Verbose narration, for a sink that wants it. Ignored by default.

        `event` reports *that* a tool ran; this reports what it was given and what came back —
        `browser_click_text "Insert column left"`, then `clicked=Insert column left`. It also
        carries an agent's own words between tool calls, under the kind `agent_say`.

        Separate from `event` because it suits a terminal and not a chat channel: the CLI wants
        every argument and outcome as it happens, while Slack wants one tidy line per step that
        does not grow. The default does nothing, so a sink opts in by overriding it and no
        existing sink changes behaviour.

        A sink that does implement this should render tool progress **from here rather than
        from `event`** — the two are emitted as a pair, and rendering both prints every call
        twice.
        """

    @abstractmethod
    async def final(self, text: str) -> None:
        """Deliver the completed answer, and close out any progress display.

        Empty text is a legitimate, silent outcome: it happens when no `llm` agents are
        registered so the orchestrator never runs, and script agents have already said
        everything there is to say. Implementations should settle their progress state
        but post nothing.
        """

    @abstractmethod
    async def error(self, text: str) -> None:
        """Report that the run failed."""

    async def settle(self) -> None:
        """Close out any progress display, after the very last event of a run.

        `final` and `error` already do this, but an `after_orchestrator` script agent
        reports progress after the answer has been delivered. This is called once at the
        end so a listener that renders live progress can leave nothing spinning. The
        default does nothing, which is right for any sink that only appends.
        """


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
