"""The listening end of the stark-browser connection.

The Chrome extension **dials out**; this listens. A browser extension's service worker cannot
hold a listening socket, so the direction is fixed — and it has a useful consequence: several
browsers (a second profile, a colleague's machine over a tunnel) can connect to one agent,
and each announces itself on arrival.

## One server, many agents

Two agents that both declare `tools: browser:` must not each try to bind port 8765. So the
server is shared per `(host, port)` and reference-counted: the first toolset to need it starts
it, the last one to close stops it. That is why this module holds process-global state, which
is otherwise the sort of thing the rest of Stark avoids.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from ...logger import get_logger

logger = get_logger("browser")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_COMMAND_TIMEOUT = 60.0
# How long a tool call waits for a browser to show up before giving up. Long enough to cover
# "I just started the agent and I am about to open Chrome", short enough not to hang a turn.
DEFAULT_CONNECT_TIMEOUT = 20.0


class BridgeError(Exception):
    """The command could not be delivered or answered, with a reason worth showing a model."""


@dataclass
class Browser:
    """One connected extension."""

    socket: Any
    label: str = "chrome"
    version: str = ""
    connected_at: float = 0.0
    pending: dict[str, asyncio.Future] = field(default_factory=dict)

    def fail_pending(self, reason: str) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(BridgeError(reason))
        self.pending.clear()


class BrowserBridge:
    """A WebSocket server the stark-browser extension connects to.

    Commands go out with an `id`; replies come back with the same one, so several tool calls
    can be in flight at once — which they will be, the moment two agents share a browser.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        token: str = "",
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ):
        self.host = host
        self.port = port
        self.token = token
        self.command_timeout = command_timeout
        self.connect_timeout = connect_timeout

        self._runner = None
        self._browsers: list[Browser] = []
        self._arrived = asyncio.Event()
        self._counter = 0
        self._users = 0

    # --- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        if self._runner is not None:
            return

        from aiohttp import web

        app = web.Application()
        app.router.add_get("/", self._handle)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        try:
            await site.start()
        except OSError as exc:
            await self._runner.cleanup()
            self._runner = None
            raise BridgeError(
                f"could not listen on {self.host}:{self.port} ({exc}). Something else is "
                f"using that port — change it with `port:` in the agent's tools block."
            ) from exc

        logger.info("Browser bridge listening on ws://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        for browser in list(self._browsers):
            browser.fail_pending("the bridge is shutting down")
            try:
                await browser.socket.close()
            except Exception:  # pragma: no cover - shutdown is best-effort
                pass
        self._browsers.clear()

        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            logger.info("Browser bridge stopped")

    @property
    def browsers(self) -> list[Browser]:
        return list(self._browsers)

    # --- the socket --------------------------------------------------------------------

    async def _handle(self, request):
        """One extension connection, for as long as it lasts."""
        from aiohttp import WSCloseCode, WSMsgType, web

        socket = web.WebSocketResponse(heartbeat=30)
        await socket.prepare(request)

        if self.token and request.query.get("token") != self.token:
            logger.warning("Browser bridge rejected a connection with a bad token")
            # Accept the handshake and *then* close with 1008, rather than refusing with a
            # 403: the extension reads 1008 as "do not retry" and stops. Failing the
            # handshake looks like an unreachable bridge, and it would reconnect forever.
            await socket.close(code=WSCloseCode.POLICY_VIOLATION, message=b"bad token")
            return socket

        browser = Browser(socket=socket, connected_at=asyncio.get_running_loop().time())
        self._browsers.append(browser)
        self._arrived.set()

        try:
            async for message in socket:
                if message.type is WSMsgType.TEXT:
                    self._receive(browser, message.data)
                elif message.type is WSMsgType.ERROR:
                    break
        finally:
            if browser in self._browsers:
                self._browsers.remove(browser)
            browser.fail_pending("the browser disconnected")
            if not self._browsers:
                self._arrived.clear()
            logger.info("Browser disconnected (%s)", browser.label)

        return socket

    def _receive(self, browser: Browser, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Browser sent malformed JSON")
            return

        kind = message.get("type")
        if kind == "hello":
            browser.label = str(message.get("browser") or "chrome")
            browser.version = str(message.get("version") or "")
            logger.info(
                "Browser connected: %s %s", message.get("extension"), browser.version
            )
            return
        if kind == "ping":
            asyncio.create_task(self._send(browser, {"type": "pong"}))
            return
        if kind == "pong":
            return

        future = browser.pending.pop(str(message.get("id")), None)
        if future is not None and not future.done():
            future.set_result(message)

    @staticmethod
    async def _send(browser: Browser, payload: dict) -> None:
        try:
            await browser.socket.send_str(json.dumps(payload))
        except Exception as exc:  # pragma: no cover - the socket died mid-send
            logger.debug("Could not write to the browser: %s", exc)

    # --- issuing commands ----------------------------------------------------------------

    async def _wait_for_browser(self) -> Browser:
        if self._browsers:
            return self._browsers[0]

        try:
            await asyncio.wait_for(self._arrived.wait(), timeout=self.connect_timeout)
        except asyncio.TimeoutError:
            raise BridgeError(
                f"no browser is connected. Load the stark-browser extension in Chrome, open "
                f"its popup, and point it at ws://{self.host}:{self.port}."
            ) from None

        if not self._browsers:  # pragma: no cover - it arrived and left again
            raise BridgeError("a browser connected and disconnected again")
        return self._browsers[0]

    async def call(self, command: str, params: dict | None = None) -> dict:
        """Send one command and wait for its answer.

        Returns the extension's `result` on success. Raises `BridgeError` with the
        extension's own message when it refuses — those messages are written for a model to
        act on, so they are passed through rather than reworded.
        """
        await self.start()
        browser = await self._wait_for_browser()

        self._counter += 1
        request_id = str(self._counter)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        browser.pending[request_id] = future

        await self._send(browser, {"id": request_id, "command": command, "params": params or {}})

        try:
            answer = await asyncio.wait_for(future, timeout=self.command_timeout)
        except asyncio.TimeoutError:
            browser.pending.pop(request_id, None)
            raise BridgeError(
                f"the browser did not answer '{command}' within {self.command_timeout:g}s"
            ) from None

        if not answer.get("ok"):
            raise BridgeError(str(answer.get("error") or "the browser refused the command"))
        return answer.get("result") or {}


# --- the shared server -----------------------------------------------------------------------

_BRIDGES: dict[tuple[str, int], BrowserBridge] = {}
_LOCK = asyncio.Lock()


async def acquire(host: str, port: int, **settings) -> BrowserBridge:
    """The bridge for this host and port, started, with one more user counted.

    Shared rather than per-agent because a port can only be bound once. Two agents with a
    `browser` toolset talk to the same extension, which is also what you want: they are
    driving one browser.
    """
    async with _LOCK:
        key = (host, port)
        bridge = _BRIDGES.get(key)
        if bridge is None:
            bridge = BrowserBridge(host=host, port=port, **settings)
            _BRIDGES[key] = bridge
        bridge._users += 1
        await bridge.start()
        return bridge


async def release(bridge: BrowserBridge) -> None:
    """Give up one user's claim, stopping the server when the last one lets go."""
    async with _LOCK:
        bridge._users -= 1
        if bridge._users > 0:
            return
        _BRIDGES.pop((bridge.host, bridge.port), None)
        await bridge.stop()
