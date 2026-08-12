from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

from ..errors import ListenerError
from ..logger import get_logger
from .base import Handler, Listener, Message, ResponseSink

logger = get_logger("slack")

BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"
APP_TOKEN_ENV = "SLACK_APP_TOKEN"

# Seconds between chat.update calls while streaming, to stay clear of Slack's rate limits.
UPDATE_INTERVAL = 1.2
THINKING = "_thinking…_"

_MENTION = re.compile(r"<@[A-Z0-9]+>")


class SlackSink(ResponseSink):
    """Posts a placeholder message, then edits it as the answer streams in."""

    def __init__(self, client: Any, channel: str, thread_ts: str | None):
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self.buffer = ""
        self.ts: str | None = None
        self._last_update = 0.0

    async def _post(self, text: str) -> None:
        response = await self.client.chat_postMessage(
            channel=self.channel,
            thread_ts=self.thread_ts,
            text=text,
        )
        self.ts = response.get("ts")

    async def _update(self, text: str) -> None:
        if self.ts is None:
            await self._post(text)
            return
        try:
            await self.client.chat_update(channel=self.channel, ts=self.ts, text=text)
        except Exception as exc:  # a failed edit should not abort the run
            logger.warning("Slack chat_update failed: %s", exc)

    async def status(self, text: str) -> None:
        if self.ts is None:
            await self._post(THINKING)

    async def chunk(self, text: str) -> None:
        self.buffer += text
        now = time.monotonic()
        if now - self._last_update < UPDATE_INTERVAL:
            return
        self._last_update = now
        await self._update(self.buffer)

    async def event(self, kind: str, detail: str) -> None:
        if kind == "agent_start" and not self.buffer:
            await self._update(f"{THINKING}\n> {detail}")

    async def final(self, text: str) -> None:
        await self._update(text or self.buffer or "(no output)")

    async def error(self, text: str) -> None:
        await self._update(f":warning: {text}")


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

        @app.event("app_mention")
        async def on_mention(event: dict[str, Any], client: Any) -> None:
            await self._dispatch(event, client)

        @app.event("message")
        async def on_message(event: dict[str, Any], client: Any) -> None:
            # Only direct messages: mentions in channels already arrive as app_mention.
            if event.get("channel_type") != "im" or event.get("subtype"):
                return
            if event.get("bot_id"):
                return
            await self._dispatch(event, client)

        self._app = app
        self._socket = AsyncSocketModeHandler(app, app_token)

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

    async def start(self) -> None:
        self._build()
        logger.info("Slack listener starting (Socket Mode)")
        await self._socket.start_async()

    async def stop(self) -> None:
        if self._socket is not None:
            try:
                await self._socket.close_async()
            except Exception as exc:  # pragma: no cover - shutdown is best-effort
                logger.debug("Slack socket close failed: %s", exc)
