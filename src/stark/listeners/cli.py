from __future__ import annotations

import asyncio
import sys
import time

from ..logger import get_logger
from .base import Handler, Listener, Message, ResponseSink

logger = get_logger("cli")

PROMPT = "\nyou › "
EXIT_WORDS = {"/exit", "/quit", "exit", "quit"}

_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _supports_color() -> bool:
    return sys.stdout.isatty()


def format_duration(seconds: float) -> str:
    """Render an elapsed time at a sensible precision for its magnitude."""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes)}m {remainder:04.1f}s"


class CLISink(ResponseSink):
    """Prints the answer to stdout as it streams in."""

    def __init__(self, show_events: bool = True):
        self.show_events = show_events
        self.color = _supports_color()
        self._started = False

    def _dim(self, text: str) -> str:
        return f"{_DIM}{text}{_RESET}" if self.color else text

    def _write(self, text: str, newline: bool = False) -> None:
        sys.stdout.write(f"{text}\n" if newline else text)
        sys.stdout.flush()

    async def status(self, text: str) -> None:
        self._write(self._dim(f"  · {text}"), newline=True)

    async def chunk(self, text: str) -> None:
        if not self._started:
            label = f"{_BOLD}stark ›{_RESET} " if self.color else "stark › "
            self._write(f"\n{label}")
            self._started = True
        self._write(text)

    async def event(self, kind: str, detail: str, key: str | None = None) -> None:
        if not self.show_events:
            return
        marker = {
            "agent_start": "→",
            "agent_end": "✓",
            "agent_error": "✗",
            "tool": "·",
            "tool_end": "✓",
        }.get(kind, "·")
        self._write(self._dim(f"  {marker} {detail}"), newline=True)

    async def message(self, text: str) -> None:
        """Print a script agent's output as its own labelled block."""
        if not text.strip():
            return
        if self._started:
            self._write("", newline=True)
            self._started = False
        label = f"{_BOLD}script ›{_RESET} " if self.color else "script › "
        self._write(f"\n{label}{text}", newline=True)

    async def final(self, text: str) -> None:
        if self._started:
            self._write("", newline=True)
            self._started = False
            return
        # An empty answer is a valid silent outcome, not something to announce.
        if not text.strip():
            return
        label = f"{_BOLD}stark ›{_RESET} " if self.color else "stark › "
        self._write(f"\n{label}{text}", newline=True)

    async def error(self, text: str) -> None:
        if self._started:
            self._write("", newline=True)
            self._started = False
        self._write(f"\n[error] {text}", newline=True)


class CLIListener(Listener):
    """An interactive terminal prompt."""

    name = "cli"

    def __init__(self, handler: Handler, roster: str = "", show_events: bool = True):
        super().__init__(handler)
        self.roster = roster
        self.show_events = show_events

    def _banner(self) -> str:
        lines = [
            "",
            "Stark — type a query, /agents to list agents, /exit to quit.",
        ]
        if self.roster:
            lines.append("")
            lines.append(self.roster)
        return "\n".join(lines)

    async def start(self) -> None:
        print(self._banner(), flush=True)

        while True:
            try:
                # input() blocks, so read it off the event loop to keep the loop free
                # for the MCP transports and any in-flight work.
                raw = await asyncio.to_thread(input, PROMPT)
            except (EOFError, KeyboardInterrupt):
                print("\nBye.", flush=True)
                return

            text = raw.strip()
            if not text:
                continue
            if text.lower() in EXIT_WORDS:
                print("Bye.", flush=True)
                return
            if text.lower() in {"/agents", "/help"}:
                print(self.roster or "No agents are registered.", flush=True)
                continue

            sink = CLISink(show_events=self.show_events)
            # Wall-clock for the whole query: every delegation, tool call and model
            # turn it took to answer.
            started = time.monotonic()
            try:
                result = await self.handler(Message(text=text, user="cli"), sink)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Unhandled error while answering")
                await sink.error(f"{type(exc).__name__}: {exc}")
                # Still report the time — a slow failure is worth seeing.
                await sink.status(format_duration(time.monotonic() - started))
                continue

            await sink.status(self._footer(result, time.monotonic() - started))

    @staticmethod
    def _footer(result, elapsed: float) -> str:
        parts = [format_duration(elapsed), f"{result.iterations} iteration(s)"]
        if result.agent_results:
            parts.append(f"{len(result.agent_results)} agent call(s)")
        if result.cost:
            parts.append(f"${result.cost:.4f}")
        return " · ".join(parts)
