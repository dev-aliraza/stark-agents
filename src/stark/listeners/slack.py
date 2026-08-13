from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from ..config import (
    CHANNEL_TYPE_EVENTS,
    DEFAULT_DONE_EMOJI,
    DEFAULT_FAILED_EMOJI,
    DEFAULT_RUNNING_EMOJI,
    DEFAULT_STARTING_LABEL,
    DEFAULT_UPDATE_INTERVAL,
    EVENT_APP_MENTION,
    EVENT_MESSAGE_IM,
    SlackConfig,
)
from ..errors import ListenerError
from ..logger import get_logger
from .base import Handler, Listener, Message, ResponseSink, trigger_values

logger = get_logger("slack")

BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"
APP_TOKEN_ENV = "SLACK_APP_TOKEN"

# Defaults, overridable per process via stark.run(config={"slack": {...}}).
UPDATE_INTERVAL = DEFAULT_UPDATE_INTERVAL
RUNNING_EMOJI = DEFAULT_RUNNING_EMOJI
DONE_EMOJI = DEFAULT_DONE_EMOJI
FAILED_EMOJI = DEFAULT_FAILED_EMOJI
STARTING_LABEL = DEFAULT_STARTING_LABEL

MAX_LABEL_CHARS = 180

# Message subtypes worth handling. Everything else — edits, deletions, channel joins,
# thread broadcasts — is not a new question, so it is ignored. `bot_message` is only
# accepted when `allow_bots` is on.
BOT_MESSAGE_SUBTYPE = "bot_message"

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

    def render(self, config: SlackConfig) -> str:
        prefix = "        ↳ " if self.parent else ""
        if self.state == RUNNING:
            return f"{prefix}{config.running_emoji} {self.label}"
        emoji = config.failed_emoji if self.state == FAILED else config.done_emoji
        # Slack mrkdwn strikethrough.
        return f"{prefix}{emoji} ~{self.label}~"


class SlackSink(ResponseSink):
    """Streams progress into one editable message, then posts the answer separately.

    The answer is never streamed. Slack readers do not benefit from watching tokens
    arrive — a half-written message is noise in a channel, and every edit costs an API
    call against a ~1/second limit. So `chunk` is deliberately ignored and the finished
    text is posted once.

    What does stream is the work: each agent delegation and tool call appears as a
    running line while it happens, and is struck through when it finishes. The emoji and
    the cooldown come from `SlackConfig`, so a deployment can supply its own.
    """

    def __init__(
        self,
        client: Any,
        channel: str,
        thread_ts: str | None,
        config: SlackConfig | None = None,
    ):
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self.config = config or SlackConfig()
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
            placeholder = Step(
                "__run__", self.config.starting_label, RUNNING if self._running else DONE
            )
            return placeholder.render(self.config)
        return "\n".join(step.render(self.config) for step in self._steps)

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
                await asyncio.sleep(self.config.update_interval)
        except asyncio.CancelledError:
            pass

    async def _flush_now(self) -> None:
        """Render the current progress immediately, leaving the coalescer running.

        Used before posting a script agent's output, so the step list above it is current.
        """
        self._dirty = False
        await self._edit_progress(self._render())

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

    async def message(self, text: str) -> None:
        """Post a script agent's output as its own message, before the final answer.

        Flushed first so the progress list is up to date when the output lands beneath it.
        """
        if not text.strip():
            return
        await self._flush_now()
        await self._post(text)

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
        # Empty is a valid silent outcome — the progress message is the whole reply.
        if not text.strip():
            return
        self.answer_ts = await self._post(text)

    async def error(self, text: str) -> None:
        await self._settle(failed=True)
        self.answer_ts = await self._post(f"{self.config.failed_emoji} {text}")

    async def settle(self) -> None:
        """Re-close the progress message after work that ran past `final`.

        An `after_orchestrator` script agent adds steps once the answer has been posted,
        which restarts the coalescer. Without this the last edit would land whenever that
        loop next ticked, or not at all if the process went idle first.
        """
        await self._settle()

    async def _settle(self, failed: bool = False) -> None:
        """Close out the progress message: nothing may be left spinning."""
        await self._stop_flusher()
        self._running = False
        for step in self._steps:
            if step.state == RUNNING:
                step.state = FAILED if failed else DONE
        await self._edit_progress(self._render())


class SlackListener(Listener):
    """Listens over Slack Socket Mode for the events its config asks for.

    Which events, and which messages within them, come from `SlackConfig.events`. By
    default that is `app_mention` alone: the bot answers when named and stays out of the
    way otherwise. Anything wider is opted into per event, optionally behind a filter.
    """

    name = "slack"

    def __init__(self, handler: Handler, config: SlackConfig | None = None):
        super().__init__(handler)
        self.config = config or SlackConfig()
        self._app: Any = None
        self._socket: Any = None
        # Learned from auth.test at startup. Used to ignore our own posts, and to drop the
        # duplicate copy of a mention that also arrives as a channel message.
        self.bot_user_id: str | None = None

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

        # Register only what the config asked for. An unregistered event is one Bolt logs
        # as unhandled, which is a clearer signal than a handler that silently returns.
        if self.config.listens_to(EVENT_APP_MENTION):

            @app.event("app_mention")
            async def on_mention(event: dict[str, Any], client: Any) -> None:
                await self.on_mention_event(event, client)

        if self.config.message_events:
            # All four message.* flavours arrive through this one handler, told apart by
            # channel_type.
            @app.event("message")
            async def on_message(event: dict[str, Any], client: Any) -> None:
                await self.on_message_event(event, client)

        self._app = app
        self._socket = AsyncSocketModeHandler(app, app_token)

    async def on_mention_event(self, event: dict[str, Any], client: Any) -> None:
        """Handle an `app_mention`, i.e. the bot named in a channel."""
        if self._from_a_bot(event, EVENT_APP_MENTION):
            return
        await self._dispatch(event, client, EVENT_APP_MENTION)

    async def on_message_event(self, event: dict[str, Any], client: Any) -> None:
        """Handle a `message`, if its flavour is one of the enabled `message.*` events."""
        channel_type = str(event.get("channel_type") or "")
        name = CHANNEL_TYPE_EVENTS.get(channel_type)

        if name is None:
            logger.debug("Ignoring message with unrecognised channel_type=%r", channel_type)
            return
        if not self.config.listens_to(name):
            logger.debug(
                "Ignoring %s: not enabled. Add it to config.slack.events to handle it.", name
            )
            return

        # Authorship is checked before the subtype, because a bot post arrives *as* the
        # `bot_message` subtype — deciding on the subtype first would drop it with a note
        # about subtypes instead of the one about `allow_bots` that actually helps.
        if self._from_a_bot(event, name):
            return

        subtype = str(event.get("subtype") or "")
        if subtype and subtype != BOT_MESSAGE_SUBTYPE:
            # Edits, deletions and joins are not new questions.
            logger.debug("Ignoring %s with subtype=%s", name, subtype)
            return
        if subtype == BOT_MESSAGE_SUBTYPE and not self.config.allow_bots:
            # A bot post with no bot_id to identify it by.
            logger.info(
                "Ignoring %s with subtype=%s; set config.slack.allow_bots to handle bot "
                "posts",
                name,
                subtype,
            )
            return
        if self._is_duplicate_of_a_mention(event, name):
            return

        await self._dispatch(event, client, name)

    def _from_a_bot(self, event: dict[str, Any], name: str) -> bool:
        """Whether to drop this event because a bot, or we ourselves, wrote it."""
        if event.get("user") and event.get("user") == self.bot_user_id:
            # Always: answering ourselves is an unbounded loop.
            logger.debug("Ignoring %s from ourselves", name)
            return True
        author = event.get("bot_id")
        if not author:
            return False
        if self.config.allow_bots:
            logger.debug("Handling %s from bot %s (allow_bots is on)", name, author)
            return False
        logger.info(
            "Ignoring %s from bot %s; set config.slack.allow_bots to handle bot posts",
            name,
            author,
        )
        return True

    def _is_duplicate_of_a_mention(self, event: dict[str, Any], name: str) -> bool:
        """Drop the channel-message copy of a mention that also arrives as `app_mention`.

        Slack sends both for the same post, so listening to `app_mention` and
        `message.channels` together would otherwise answer it twice.
        """
        if not self.config.listens_to(EVENT_APP_MENTION) or self.bot_user_id is None:
            return False
        if f"<@{self.bot_user_id}>" not in str(event.get("text") or ""):
            return False
        logger.debug("Ignoring %s that mentions us; app_mention covers it", name)
        return True

    async def _dispatch(self, event: dict[str, Any], client: Any, name: str) -> None:
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
            meta={"event": event, "slack_event": name},
        )

        # The filter runs against the same text the handler will see — mention stripped —
        # so a rule reads the way it looks. No match means total silence: no progress
        # message, nothing posted.
        rule = self.config.filter_for(name)
        if rule is not None and not rule.matches(trigger_values(message)):
            logger.debug("Ignoring %s: filter %s did not match", name, rule)
            return

        sink = SlackSink(client, channel, thread_ts, self.config)
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

        self.bot_user_id = auth.get("user_id")
        logger.info(
            "Slack connected as %s (bot user %s) in workspace '%s'",
            auth.get("user"),
            self.bot_user_id,
            auth.get("team"),
        )
        logger.info("Slack listening for: %s", self.config.describe_events())
        if not self.config.listens_to(EVENT_MESSAGE_IM):
            logger.info(
                "Direct messages are ignored — add 'message.im' to config.slack.events "
                "to handle them."
            )

        granted = self._granted_scopes(auth)
        if granted is None:
            return

        required = self.config.required_scopes
        missing = [scope for scope in required if scope not in granted]
        if missing:
            logger.error(
                "Slack bot token is missing the scope(s) %s, needed for the event(s) you "
                "configured. Add them under OAuth & Permissions, then REINSTALL the app — "
                "scope changes need a reinstall to take effect. Granted: %s",
                ", ".join(missing),
                ", ".join(sorted(granted)) or "(none)",
            )
        else:
            logger.info(
                "Slack scopes OK (%s). Waiting for events — the app must also be "
                "subscribed to the %s bot event(s) under Event Subscriptions, and invited "
                "to any channel you expect it to read.",
                ", ".join(required),
                ", ".join(self.config.enabled_events),
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
