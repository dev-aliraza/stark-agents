from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from ..errors import ListenerError
from ..logger import get_logger
from .base import Handler, Listener, Message, ResponseSink

logger = get_logger("slack")

BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"
APP_TOKEN_ENV = "SLACK_APP_TOKEN"

# Seconds between chat.update calls, to stay clear of Slack's ~1/second edit limit.
UPDATE_INTERVAL = 1.2

RUNNING_EMOJI = ":loading123:"
DONE_EMOJI = ":talabatdone:"
FAILED_EMOJI = ":warning:"

STARTING_LABEL = "Working on it"
MAX_LABEL_CHARS = 180

# Bot-token scopes the listener needs: receive mentions, receive DMs, and reply.
REQUIRED_SCOPES = ("app_mentions:read", "chat:write", "im:history")

_MENTION = re.compile(r"<@[A-Z0-9]+>")
_WHITESPACE = re.compile(r"\s+")

RUNNING = "running"
DONE = "done"
FAILED = "failed"


def _label(text: str) -> str:
    """Flatten a label onto one line so it cannot break the progress list.

    Task text comes from the model and may span lines or contain `~`, which would
    otherwise corrupt Slack's strikethrough markup.
    """
    flattened = _WHITESPACE.sub(" ", text).strip().replace("~", "-")
    if len(flattened) > MAX_LABEL_CHARS:
        flattened = f"{flattened[: MAX_LABEL_CHARS - 1].rstrip()}…"
    return flattened


@dataclass
class Step:
    """One agent delegation or tool call, as shown in the progress message."""

    key: str
    label: str
    state: str = RUNNING
    parent: str | None = None

    def render(self) -> str:
        prefix = "        ↳ " if self.parent else ""
        if self.state == RUNNING:
            return f"{prefix}{RUNNING_EMOJI} {self.label}"
        emoji = FAILED_EMOJI if self.state == FAILED else DONE_EMOJI
        # Slack mrkdwn strikethrough.
        return f"{prefix}{emoji} ~{self.label}~"


class SlackSink(ResponseSink):
    """Streams progress into one editable message, then posts the answer separately.

    The answer is never streamed. Slack readers do not benefit from watching tokens
    arrive — a half-written message is noise in a channel, and every edit costs an API
    call against a ~1/second limit. So `chunk` is deliberately ignored and the finished
    text is posted once.

    What does stream is the work: each agent delegation and tool call appears as a
    `:loading123:` line while it runs, and is struck through with `:talabatdone:` when it
    finishes.
    """

    def __init__(self, client: Any, channel: str, thread_ts: str | None):
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self.progress_ts: str | None = None
        self.answer_ts: str | None = None
        self._steps: list[Step] = []
        self._by_key: dict[str, Step] = {}
        self._running = True
        self._dirty = False
        self._flusher: asyncio.Task[None] | None = None

    # --- Slack plumbing ---------------------------------------------------------------

    async def _post(self, text: str) -> str | None:
        response = await self.client.chat_postMessage(
            channel=self.channel,
            thread_ts=self.thread_ts,
            text=text,
        )
        return response.get("ts")

    async def _edit_progress(self, text: str) -> None:
        if self.progress_ts is None:
            self.progress_ts = await self._post(text)
            return
        try:
            await self.client.chat_update(
                channel=self.channel, ts=self.progress_ts, text=text
            )
        except Exception as exc:  # a failed edit must not abort the run
            logger.warning("Slack chat_update failed: %s", exc)

    def _render(self) -> str:
        if not self._steps:
            # Nothing was delegated, so show the run itself as the single step.
            placeholder = Step("__run__", STARTING_LABEL, RUNNING if self._running else DONE)
            return placeholder.render()
        return "\n".join(step.render() for step in self._steps)

    def _touch(self) -> None:
        """Mark the progress list dirty and make sure something will render it.

        Renders are coalesced rather than rate-limited: the first change goes out
        immediately, and changes arriving during the cooldown collapse into one edit
        afterwards. A plain time gate would drop the render entirely, so a step that
        starts and finishes inside one interval would never show as running.
        """
        self._dirty = True
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        try:
            while self._dirty:
                self._dirty = False
                await self._edit_progress(self._render())
                await asyncio.sleep(UPDATE_INTERVAL)
        except asyncio.CancelledError:
            pass

    async def _stop_flusher(self) -> None:
        if self._flusher is not None and not self._flusher.done():
            self._flusher.cancel()
            try:
                await self._flusher
            except asyncio.CancelledError:
                pass
        self._flusher = None
        self._dirty = False

    # --- ResponseSink -----------------------------------------------------------------

    async def status(self, text: str) -> None:
        """Acknowledge the message immediately, before any work has happened."""
        await self._edit_progress(self._render())

    async def chunk(self, text: str) -> None:
        """Ignored: the answer is posted once, not streamed."""

    async def event(self, kind: str, detail: str, key: str | None = None) -> None:
        if kind in {"agent_start", "tool"}:
            self._begin(kind, detail, key)
        elif kind in {"agent_end", "tool_end"}:
            self._finish(key, detail, DONE)
        elif kind == "agent_error":
            self._finish(key, detail, FAILED)
        else:
            return
        self._touch()

    def _begin(self, kind: str, detail: str, key: str | None) -> None:
        step_key = key or f"{kind}:{len(self._steps)}"
        if step_key in self._by_key:
            return

        # Tool keys are "<agent key>:<tool call id>", so the agent that ran the tool is
        # the prefix. Nesting the line under that agent keeps a parallel run readable.
        parent = None
        if kind == "tool" and step_key.rpartition(":")[0] in self._by_key:
            parent = step_key.rpartition(":")[0]

        step = Step(step_key, _label(detail), parent=parent)
        self._by_key[step_key] = step
        self._steps.insert(self._insertion_index(parent), step)

    def _insertion_index(self, parent: str | None) -> int:
        """Place a child directly after its parent's existing children."""
        if parent is None:
            return len(self._steps)
        index = len(self._steps)
        for position, step in enumerate(self._steps):
            if step.key == parent or step.parent == parent:
                index = position + 1
        return index

    def _finish(self, key: str | None, detail: str, state: str) -> None:
        step = self._by_key.get(key or "")
        if step is None:
            # An end without a matching start: record it so the work is still visible.
            step = Step(key or f"end:{len(self._steps)}", _label(detail))
            self._steps.append(step)
            self._by_key[step.key] = step
        step.state = state

    async def final(self, text: str) -> None:
        await self._settle()
        self.answer_ts = await self._post(text.strip() or "_(no output)_")

    async def error(self, text: str) -> None:
        await self._settle(failed=True)
        self.answer_ts = await self._post(f"{FAILED_EMOJI} {text}")

    async def _settle(self, failed: bool = False) -> None:
        """Close out the progress message: nothing may be left spinning."""
        await self._stop_flusher()
        self._running = False
        for step in self._steps:
            if step.state == RUNNING:
                step.state = FAILED if failed else DONE
        await self._edit_progress(self._render())


class SlackListener(Listener):
    """Listens for mentions and DMs over Slack Socket Mode."""

    name = "slack"

    def __init__(self, handler: Handler):
        super().__init__(handler)
        self._app: Any = None
        self._socket: Any = None

    @staticmethod
    def _imports() -> tuple[Any, Any]:
        try:
            from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
            from slack_bolt.app.async_app import AsyncApp
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ListenerError(
                "the slack listener needs slack-bolt: pip install 'stark-agents[slack]'"
            ) from exc
        return AsyncApp, AsyncSocketModeHandler

    @classmethod
    def preflight(cls) -> tuple[str, str]:
        """Verify slack-bolt is installed and both tokens are present."""
        cls._imports()

        bot_token = os.environ.get(BOT_TOKEN_ENV, "")
        app_token = os.environ.get(APP_TOKEN_ENV, "")
        missing = [
            name
            for name, value in ((BOT_TOKEN_ENV, bot_token), (APP_TOKEN_ENV, app_token))
            if not value
        ]
        if missing:
            raise ListenerError(
                f"the slack listener needs {' and '.join(missing)} in the environment"
            )
        return bot_token, app_token

    def _build(self) -> None:
        AsyncApp, AsyncSocketModeHandler = self._imports()
        bot_token, app_token = self.preflight()

        app = AsyncApp(token=bot_token)

        @app.middleware
        async def trace(body: dict[str, Any], next: Any) -> Any:
            """Log every inbound request before any listener matches it.

            Without this there is no way to tell "Slack sent nothing" apart from "Slack
            sent something and a filter dropped it" — the two have very different fixes.
            """
            event = (body or {}).get("event") or {}
            logger.debug(
                "Slack request: type=%s event=%s channel_type=%s subtype=%s",
                (body or {}).get("type"),
                event.get("type"),
                event.get("channel_type"),
                event.get("subtype"),
            )
            return await next()

        @app.event("app_mention")
        async def on_mention(event: dict[str, Any], client: Any) -> None:
            await self.on_mention_event(event, client)

        @app.event("message")
        async def on_message(event: dict[str, Any], client: Any) -> None:
            await self.on_message_event(event, client)

        self._app = app
        self._socket = AsyncSocketModeHandler(app, app_token)

    async def on_mention_event(self, event: dict[str, Any], client: Any) -> None:
        """Handle an `app_mention`, i.e. the bot named in a channel."""
        if event.get("bot_id"):
            # Another bot mentioned us; answering could start a loop between them.
            logger.info("Ignoring app_mention from bot %s", event.get("bot_id"))
            return
        await self._dispatch(event, client)

    async def on_message_event(self, event: dict[str, Any], client: Any) -> None:
        """Handle a `message`, but only a direct one.

        Mentions in channels already arrive as `app_mention`, so accepting channel
        messages here would answer them twice.
        """
        if event.get("channel_type") != "im":
            logger.debug(
                "Ignoring message in channel_type=%s (only DMs are handled here; "
                "channel mentions arrive as app_mention)",
                event.get("channel_type"),
            )
            return
        if event.get("subtype"):
            logger.debug("Ignoring message with subtype=%s", event.get("subtype"))
            return
        if event.get("bot_id"):
            logger.debug("Ignoring message from bot %s", event.get("bot_id"))
            return
        await self._dispatch(event, client)

    async def _dispatch(self, event: dict[str, Any], client: Any) -> None:
        text = _MENTION.sub("", str(event.get("text") or "")).strip()
        channel = str(event.get("channel") or "")
        if not text or not channel:
            return

        thread_ts = event.get("thread_ts") or event.get("ts")
        message = Message(
            text=text,
            user=event.get("user"),
            channel=channel,
            thread=thread_ts,
            meta={"event": event},
        )
        sink = SlackSink(client, channel, thread_ts)
        await sink.status("working")

        try:
            await self.handler(message, sink)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Unhandled error while answering a Slack message")
            await sink.error(f"{type(exc).__name__}: {exc}")

    async def _log_identity(self) -> None:
        """Confirm the bot token works, say who we are, and check the scopes.

        A silent bot is nearly always configuration rather than code, and the two
        checkable causes are a rejected token and a missing scope. Slack returns the
        granted scopes in the auth.test response headers, so both can be reported at
        startup instead of leaving you to guess from silence.
        """
        try:
            auth = await self._app.client.auth_test()
        except Exception as exc:
            logger.error(
                "Slack auth.test failed — the bot token is rejected, so no events will "
                "be handled: %s",
                exc,
            )
            return

        logger.info(
            "Slack connected as %s (bot user %s) in workspace '%s'",
            auth.get("user"),
            auth.get("user_id"),
            auth.get("team"),
        )

        granted = self._granted_scopes(auth)
        if granted is None:
            return

        missing = [scope for scope in REQUIRED_SCOPES if scope not in granted]
        if missing:
            logger.error(
                "Slack bot token is missing the scope(s) %s. Add them under OAuth & "
                "Permissions, then REINSTALL the app — scope changes need a reinstall to "
                "take effect. Granted: %s",
                ", ".join(missing),
                ", ".join(sorted(granted)) or "(none)",
            )
        else:
            logger.info(
                "Slack scopes OK. Waiting for events — the app must also be subscribed to "
                "the 'app_mention' and 'message.im' bot events under Event Subscriptions, "
                "and invited to any channel you mention it in."
            )

    @staticmethod
    def _granted_scopes(auth: Any) -> set[str] | None:
        """Pull granted scopes out of the auth.test response headers."""
        headers = getattr(auth, "headers", None) or {}
        raw = headers.get("x-oauth-scopes") or headers.get("X-OAuth-Scopes")
        if isinstance(raw, list):  # some clients expose header values as lists
            raw = raw[0] if raw else ""
        if not raw:
            logger.debug("Slack did not report granted scopes; skipping the scope check")
            return None
        return {scope.strip() for scope in str(raw).split(",") if scope.strip()}

    async def start(self) -> None:
        self._build()
        await self._log_identity()
        logger.info("Slack listener starting (Socket Mode)")
        await self._socket.start_async()

    async def stop(self) -> None:
        if self._socket is not None:
            try:
                await self._socket.close_async()
            except Exception as exc:  # pragma: no cover - shutdown is best-effort
                logger.debug("Slack socket close failed: %s", exc)
