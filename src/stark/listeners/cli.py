from __future__ import annotations

import asyncio
import select
import sys
import time

from ..logger import get_logger
from .base import Handler, Listener, Message, ResponseSink

logger = get_logger("cli")

PROMPT = "\nyou › "
CONTINUATION = "  … "
EXIT_WORDS = {"/exit", "/quit", "exit", "quit"}

# Type this alone on a line to open a block, and again to send it. For pasting you need
# nothing — see `read_query`.
BLOCK_DELIMITER = '"""'

# How long to wait for the rest of a paste before deciding the input is finished. A paste
# arrives as one burst but the terminal may hand it over in chunks, so zero is too eager;
# a human cannot type the next line inside this window, so it never joins two real prompts.
PASTE_SETTLE = 0.05

_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _supports_color() -> bool:
    return sys.stdout.isatty()


def stdin_pending(timeout: float = PASTE_SETTLE) -> bool:
    """Whether more input is already waiting to be read.

    Only meaningful for a terminal. When stdin is a pipe or a file everything is available
    at once, so this would report "pending" for the whole stream and swallow it into one
    query — which is why a non-tty always answers False and keeps the old line-per-query
    behaviour for `echo … | stark`.
    """
    try:
        if not sys.stdin.isatty():
            return False
        return bool(select.select([sys.stdin], [], [], timeout)[0])
    except (OSError, ValueError):  # a closed or unselectable stdin
        return False


def read_query(prompt: str, more_pending=None) -> str:
    """Read one query, however many lines the user needed to express it.

    `input()` returns a single line, and a pasted newline is indistinguishable from a typed
    Enter — the terminal even maps a bare carriage return to one. So pasting a multi-line
    prompt used to submit its first line as a whole query and each following line as another
    query of its own.

    Two ways out, and neither asks anything of someone typing a one-liner:

    * **Pasting just works.** After the first line, if more input is already buffered it can
      only be the rest of a burst, so keep reading until stdin goes quiet and join it back
      together.
    * **Typing a block** — open with `\"\"\"` on its own line, close with the same.
    """
    more_pending = stdin_pending if more_pending is None else more_pending

    first = input(prompt)
    if first.strip() == BLOCK_DELIMITER:
        return _read_until_delimiter()

    if not more_pending():
        return first

    lines = [first]
    while more_pending():
        try:
            lines.append(input())
        except EOFError:  # the paste ended without a trailing newline
            break
    return "\n".join(lines)


def _read_until_delimiter() -> str:
    """Collect lines until a closing `\"\"\"`, for a prompt typed rather than pasted."""
    lines: list[str] = []
    while True:
        try:
            line = input(CONTINUATION)
        except EOFError:  # Ctrl-D closes the block rather than losing it
            break
        if line.strip() == BLOCK_DELIMITER:
            break
        lines.append(line)
    return "\n".join(lines)


class BasicReader:
    """Reads with `input()`, joining a burst of lines into one query.

    The fallback, used when prompt_toolkit is not installed. It stops a pasted prompt from
    being split into one query per line, but it cannot let you *review* the paste first:
    `input()` returns as soon as it has a line, so the block is submitted the moment the
    burst ends. For paste-then-edit you need `EditorReader`.
    """

    multiline_paste = False

    async def read(self, prompt: str) -> str:
        # input() blocks, so read it off the event loop to keep the loop free for the MCP
        # transports and any in-flight work.
        return await asyncio.to_thread(read_query, prompt)

    def hint(self) -> str:
        return (
            'A pasted prompt is taken whole, but sends at once; to type one, open and close '
            'with """. Install prompt_toolkit to paste, edit, then send.'
        )


class EditorReader:
    """Reads with prompt_toolkit, so a paste lands in a buffer you can edit.

    prompt_toolkit implements **bracketed paste** itself rather than relying on readline —
    which matters, because macOS links Python's `readline` against libedit, and libedit has
    no bracketed-paste support at all. With it, the terminal brackets the pasted text and
    the newlines inside arrive as *text*, not as Enter. So a paste sits in the buffer, you
    read and change it, and Enter sends.

    `multiline=False` is deliberate: Enter must submit, because that is what a prompt is for.
    Alt+Enter inserts a newline for the times you want to type a block by hand.
    """

    multiline_paste = True

    def __init__(self, session):
        self.session = session

    @classmethod
    def build(cls) -> "EditorReader | None":
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.key_binding import KeyBindings
        except ImportError:
            return None

        bindings = KeyBindings()

        @bindings.add("escape", "enter")
        def _newline(event) -> None:
            """Alt+Enter (or Esc then Enter) — a deliberate line break, not a send."""
            event.current_buffer.insert_text("\n")

        try:
            return cls(PromptSession(key_bindings=bindings))
        except Exception as exc:  # pragma: no cover - no usable terminal
            logger.debug("prompt_toolkit is installed but unusable here: %s", exc)
            return None

    async def read(self, prompt: str) -> str:
        # prompt_async runs on the current loop, so no worker thread is involved.
        return await self.session.prompt_async(prompt)

    def hint(self) -> str:
        return (
            "Paste a multi-line prompt, edit it, then press Enter to send. "
            "Alt+Enter adds a line without sending."
        )


def build_reader():
    """The best input reader for this environment.

    prompt_toolkit needs a terminal to draw on, so a piped or redirected stdin — `echo … |
    stark`, a test harness — gets the plain reader. Asking a line editor to edit a pipe is
    not a thing.
    """
    try:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    except (OSError, ValueError):  # a closed stdin
        interactive = False

    if not interactive:
        return BasicReader()
    return EditorReader.build() or BasicReader()


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
        # The call line printed for each in-flight tool, so a result that adds nothing can be
        # recognised and skipped rather than printed twice.
        self._last_tool: dict[str | None, str] = {}

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
        """Agent-level progress. Tool lines come through `detail` instead, so they are not
        rendered twice — see `ResponseSink.detail`."""
        if not self.show_events or not detail.strip():
            return
        if kind in {"tool", "tool_end"}:
            return

        marker = {"agent_start": "→", "agent_end": "✓", "agent_error": "✗"}.get(kind, "·")
        self._write(self._dim(f"  {marker} {self._wrap(detail, '  ')}"), newline=True)

    async def detail(self, kind: str, text: str, key: str | None = None) -> None:
        """The verbose narration: what a tool was given, and what came back.

        Terminal-only on purpose. Slack leaves this unimplemented, so a chat channel keeps one
        tidy line per step instead of a running commentary.
        """
        if not self.show_events or not text.strip():
            return

        if kind == "tool":
            self._last_tool[key] = text
        elif kind == "tool_end":
            called = self._last_tool.pop(key, "")
            # The call is on the line directly above, so print only the outcome — repeating a
            # long call line to append eight characters of result is unreadable.
            if text == called:
                return

        marker = "✗" if text.startswith("[error]") else {"tool": "·"}.get(kind, "✓")
        if kind == "agent_say":
            marker = "»"

        # A step further in than agent-level lines, so a delegated run reads as nested.
        self._write(self._dim(f"    {marker} {self._wrap(text, '    ')}"), newline=True)

    def _wrap(self, text: str, indent: str) -> str:
        """Keep continuation lines in the same column, or a checklist falls out of it."""
        return f"\n{indent}  ".join(text.strip().splitlines())

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

    def __init__(self, handler: Handler, roster: str = "", show_events: bool = True, reader=None):
        super().__init__(handler)
        self.roster = roster
        self.show_events = show_events
        self.reader = reader or build_reader()

    def _banner(self) -> str:
        lines = [
            "",
            "Stark — type a query, /agents to list agents, /exit to quit.",
            self.reader.hint(),
        ]
        if self.roster:
            lines.append("")
            lines.append(self.roster)
        return "\n".join(lines)

    async def start(self) -> None:
        print(self._banner(), flush=True)

        while True:
            try:
                raw = await self.reader.read(PROMPT)
            except (EOFError, KeyboardInterrupt):
                print("\nBye.", flush=True)
                return

            # Only the ends are stripped: a multi-line prompt keeps its own line breaks.
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
        if result.stopped:
            # Otherwise a halted run looks identical to one that had nothing to say.
            parts.append(f"stopped by {result.stopped_by}")
        return " · ".join(parts)
