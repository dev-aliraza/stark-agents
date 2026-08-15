"""The `browser` toolset and the bridge the stark-browser extension connects to.

These are real sockets on a real ephemeral port, driven by a fake extension that speaks the
same protocol as the Chrome one. Nothing leaves the machine and no browser is involved — the
half being tested is Stark's, and the extension's half is a stub that answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket

import pytest

from stark.tools.browser import BROWSER_TOOL_NAMES, BridgeError, BrowserTools
from stark.tools.browser.bridge import BrowserBridge, acquire, release

aiohttp = pytest.importorskip("aiohttp", reason="the browser tool needs aiohttp")


def free_port() -> int:
    """A port nobody is using, so tests can run in parallel and on a busy machine."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class FakeExtension:
    """Stands in for the Chrome extension: connects out, answers commands.

    `replies` maps a command to either a dict (returned as the result) or a string (returned
    as an error), so a test can make the extension refuse exactly as the real one would.
    """

    def __init__(self, url: str, replies: dict | None = None):
        self.url = url
        self.replies = replies or {}
        self.received: list[dict] = []
        self.session = None
        self.socket = None
        self._task = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        self.socket = await self.session.ws_connect(self.url)
        await self.socket.send_str(
            json.dumps({"type": "hello", "extension": "Fake", "version": "9.9", "browser": "chrome"})
        )
        self._task = asyncio.create_task(self._pump())
        await asyncio.sleep(0.05)  # let the hello land
        return self

    async def __aexit__(self, *_):
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self.socket:
            await self.socket.close()
        if self.session:
            await self.session.close()

    async def _pump(self):
        async for message in self.socket:
            if message.type is not aiohttp.WSMsgType.TEXT:
                continue
            request = json.loads(message.data)
            if request.get("type"):
                continue

            self.received.append(request)
            reply = self.replies.get(request["command"], {"echo": request["command"]})
            if isinstance(reply, str):
                answer = {"id": request["id"], "ok": False, "error": reply}
            else:
                answer = {"id": request["id"], "ok": True, "result": reply}
            await self.socket.send_str(json.dumps(answer))


@pytest.fixture()
async def bridge():
    built = BrowserBridge(port=free_port(), connect_timeout=2, command_timeout=5)
    await built.start()
    yield built
    await built.stop()


def endpoint(bridge: BrowserBridge) -> str:
    return f"http://{bridge.host}:{bridge.port}/"


# --- the bridge --------------------------------------------------------------------------


async def test_an_extension_can_connect_and_announce_itself(bridge):
    async with FakeExtension(endpoint(bridge)):
        assert len(bridge.browsers) == 1
        assert bridge.browsers[0].version == "9.9"


async def test_a_command_round_trips(bridge):
    async with FakeExtension(endpoint(bridge), {"tabs.create": {"tabId": 42}}) as extension:
        result = await bridge.call("tabs.create", {"url": "https://example.com"})

    assert result == {"tabId": 42}
    assert extension.received[0]["command"] == "tabs.create"
    assert extension.received[0]["params"] == {"url": "https://example.com"}


async def test_the_extensions_own_error_is_passed_through(bridge):
    """Those messages are written for a model to act on, so they must not be reworded."""
    refusal = "tab 42 was not opened by this extension, so it cannot be read or changed."
    async with FakeExtension(endpoint(bridge), {"click": refusal}):
        with pytest.raises(BridgeError, match="was not opened by this extension"):
            await bridge.call("click", {"tabId": 42, "ref": "ref_1"})


async def test_a_wrong_token_is_closed_with_1008_so_the_extension_gives_up():
    """1008 is what the extension reads as "do not retry".

    Refusing the handshake with a 403 instead is indistinguishable from an unreachable
    bridge, and the extension would reconnect against the wrong token forever.
    """
    guarded = BrowserBridge(port=free_port(), token="secret")
    await guarded.start()
    try:
        async with aiohttp.ClientSession() as session:
            socket = await session.ws_connect(f"{endpoint(guarded)}?token=wrong")
            assert await socket.receive() and socket.close_code == 1008
        assert guarded.browsers == []
    finally:
        await guarded.stop()


async def test_the_right_token_connects():
    guarded = BrowserBridge(port=free_port(), token="secret")
    await guarded.start()
    try:
        async with FakeExtension(f"{endpoint(guarded)}?token=secret"):
            assert len(guarded.browsers) == 1
    finally:
        await guarded.stop()


async def test_several_commands_can_be_in_flight_at_once(bridge):
    """Replies are matched by id, so two agents sharing a browser do not cross wires."""
    async with FakeExtension(endpoint(bridge), {"a": {"which": "a"}, "b": {"which": "b"}}):
        first, second = await asyncio.gather(bridge.call("a"), bridge.call("b"))

    assert (first["which"], second["which"]) == ("a", "b")


async def test_no_browser_connected_says_what_to_do(bridge):
    with pytest.raises(BridgeError, match="Load the stark-browser extension"):
        await bridge.call("tabs.list")


async def test_the_error_names_the_endpoint_to_configure(bridge):
    with pytest.raises(BridgeError, match=f"ws://127.0.0.1:{bridge.port}"):
        await bridge.call("tabs.list")


async def test_a_disconnect_fails_the_calls_waiting_on_it(bridge):
    """Otherwise a closed browser leaves a tool call hanging until its timeout."""
    extension = FakeExtension(endpoint(bridge))
    await extension.__aenter__()

    # Silence the fake before issuing anything, or it answers before it can be closed.
    extension._task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await extension._task

    async def close_soon():
        await asyncio.sleep(0.1)
        await extension.socket.close()

    closing = asyncio.create_task(close_soon())  # held, or it can be collected mid-flight
    try:
        with pytest.raises(BridgeError, match="disconnected"):
            await bridge.call("never_answered")
    finally:
        await closing
        await extension.__aexit__()


async def test_a_slow_browser_times_out_rather_than_hanging():
    built = BrowserBridge(port=free_port(), connect_timeout=2, command_timeout=0.3)
    await built.start()
    try:
        extension = FakeExtension(endpoint(built))
        await extension.__aenter__()
        extension._task.cancel()  # connected, but will never answer

        with pytest.raises(BridgeError, match="did not answer"):
            await built.call("read_page", {"tabId": 1})
        await extension.__aexit__()
    finally:
        await built.stop()


async def test_a_port_already_in_use_says_which_setting_to_change():
    first = BrowserBridge(port=free_port())
    await first.start()
    try:
        second = BrowserBridge(port=first.port)
        with pytest.raises(BridgeError, match="port:"):
            await second.start()
    finally:
        await first.stop()


# --- the shared server ---------------------------------------------------------------------


async def test_two_toolsets_share_one_server():
    """A port can only be bound once, so two agents with a browser share the bridge."""
    port = free_port()
    one = await acquire("127.0.0.1", port)
    two = await acquire("127.0.0.1", port)
    try:
        assert one is two
    finally:
        await release(one)
        await release(two)


async def test_the_server_stops_when_the_last_user_lets_go():
    port = free_port()
    one = await acquire("127.0.0.1", port)
    two = await acquire("127.0.0.1", port)

    await release(one)
    assert one._runner is not None, "still in use by the second toolset"

    await release(two)
    assert one._runner is None

    # And the port is genuinely free again.
    again = await acquire("127.0.0.1", port)
    await release(again)


# --- the toolset ------------------------------------------------------------------------------


def toolset(**settings) -> BrowserTools:
    return BrowserTools(None, settings)


def test_the_toolset_offers_the_documented_tools():
    names = {schema["function"]["name"] for schema in toolset().schemas()}

    assert names == set(BROWSER_TOOL_NAMES)
    assert {"browser_open", "browser_text", "browser_elements", "browser_fill"} <= names


def test_the_toolset_claims_only_its_own_tools():
    tools = toolset()
    assert tools.owns("browser_open") is True
    assert tools.owns("websearch_open") is False


def test_open_is_the_only_tool_that_needs_no_tab():
    """Every other tool acts on a tab the agent already opened."""
    required = {
        schema["function"]["name"]: schema["function"]["parameters"].get("required", [])
        for schema in toolset().schemas()
    }
    assert "tabId" not in required["browser_open"]
    assert required["browser_tabs"] == []
    for name in ("browser_text", "browser_elements", "browser_click", "browser_fill"):
        assert "tabId" in required[name], name


def test_settings_are_read_from_the_tools_block():
    tools = toolset(host="0.0.0.0", port=9999, token="secret", timeout=5, connect_timeout=1)
    assert (tools.host, tools.port, tools.token) == ("0.0.0.0", 9999, "secret")
    assert (tools.command_timeout, tools.connect_timeout) == (5.0, 1.0)


def test_settings_fall_back_to_defaults():
    tools = toolset()
    assert (tools.host, tools.port) == ("127.0.0.1", 8765)


# --- argument checking, before anything reaches the browser -------------------------------------


async def test_a_missing_tab_id_is_refused_with_advice():
    result = await toolset().call("browser_text", {})
    assert "'tabId' is required" in result
    assert "browser_open" in result


async def test_a_non_numeric_tab_id_is_refused():
    assert "'tabId' is required" in await toolset().call("browser_click", {"tabId": "the first"})


async def test_a_missing_ref_points_at_where_to_get_one():
    result = await toolset().call("browser_click", {"tabId": 1})
    assert "browser_elements" in result


async def test_fill_requires_a_value():
    assert "'value' is required" in await toolset().call(
        "browser_fill", {"tabId": 1, "ref": "ref_1"}
    )


async def test_open_requires_a_url():
    assert "'url' is required" in await toolset().call("browser_open", {})


async def test_an_unknown_tool_is_reported():
    assert "unknown browser tool" in await toolset().call("browser_nope", {})


# --- end to end, toolset through bridge to a fake extension ------------------------------------


async def test_opening_a_tab_and_reading_it():
    """The article-reading path: open, then read text."""
    port = free_port()
    tools = toolset(port=port, connect_timeout=2, command_timeout=5)
    bridge = await tools._connect()

    replies = {
        "tabs.create": {"tabId": 7, "url": "https://news.example/story", "title": "A story"},
        "get_text": {"url": "https://news.example/story", "text": "The article body."},
    }
    try:
        async with FakeExtension(endpoint(bridge), replies) as extension:
            opened = json.loads(await tools.call("browser_open", {"url": "https://news.example/story"}))
            assert opened["tabId"] == 7

            read = json.loads(await tools.call("browser_text", {"tabId": 7}))
            assert read["text"] == "The article body."

        assert [item["command"] for item in extension.received] == ["tabs.create", "get_text"]
    finally:
        await tools.aclose()


async def test_filling_a_form_end_to_end():
    """The form path: elements, fill, click."""
    port = free_port()
    tools = toolset(port=port, connect_timeout=2, command_timeout=5)
    bridge = await tools._connect()

    replies = {
        "tabs.create": {"tabId": 3},
        "read_page": {"elements": [{"ref": "ref_1", "role": "input", "name": "Email"}]},
        "fill": {"filled": "ref_1"},
        "click": {"clicked": "ref_2", "navigated": True},
    }
    try:
        async with FakeExtension(endpoint(bridge), replies) as extension:
            await tools.call("browser_open", {"url": "https://example.com/signup"})
            await tools.call("browser_elements", {"tabId": 3})
            await tools.call("browser_fill", {"tabId": 3, "ref": "ref_1", "value": "a@b.com"})
            await tools.call("browser_click", {"tabId": 3, "ref": "ref_2"})

        sent = {item["command"]: item["params"] for item in extension.received}
        assert sent["fill"] == {"tabId": 3, "ref": "ref_1", "value": "a@b.com"}
        assert sent["click"] == {"tabId": 3, "ref": "ref_2"}
    finally:
        await tools.aclose()


async def test_elements_carries_the_refs_are_stale_warning():
    """A model reusing a ref after a click is the commonest way this goes wrong."""
    port = free_port()
    tools = toolset(port=port, connect_timeout=2, command_timeout=5)
    bridge = await tools._connect()
    try:
        async with FakeExtension(endpoint(bridge), {"read_page": {"elements": []}}):
            result = json.loads(await tools.call("browser_elements", {"tabId": 1}))
        assert "call browser_elements again" in result["note"]
    finally:
        await tools.aclose()


async def test_an_empty_page_says_it_might_still_be_loading():
    port = free_port()
    tools = toolset(port=port, connect_timeout=2, command_timeout=5)
    bridge = await tools._connect()
    try:
        async with FakeExtension(endpoint(bridge), {"get_text": {"text": "  "}}):
            result = json.loads(await tools.call("browser_text", {"tabId": 1}))
        assert "still be loading" in result["note"]
    finally:
        await tools.aclose()


async def test_a_refusal_from_the_extension_reaches_the_model_intact():
    port = free_port()
    tools = toolset(port=port, connect_timeout=2, command_timeout=5)
    bridge = await tools._connect()
    refusal = "ref_5 looks like a credential field, which this extension will not fill."
    try:
        async with FakeExtension(endpoint(bridge), {"fill": refusal}):
            result = await tools.call(
                "browser_fill", {"tabId": 1, "ref": "ref_5", "value": "hunter2"}
            )
        assert result.startswith("[error]")
        assert "credential field" in result
    finally:
        await tools.aclose()


async def test_window_new_is_passed_through():
    port = free_port()
    tools = toolset(port=port, connect_timeout=2, command_timeout=5)
    bridge = await tools._connect()
    try:
        async with FakeExtension(endpoint(bridge), {"tabs.create": {"tabId": 1}}) as extension:
            await tools.call("browser_open", {"url": "https://e.com", "window": "new"})
        assert extension.received[0]["params"]["window"] == "new"
    finally:
        await tools.aclose()


async def test_closing_the_toolset_releases_the_bridge():
    port = free_port()
    tools = toolset(port=port)
    bridge = await tools._connect()
    assert bridge._runner is not None

    await tools.aclose()
    assert bridge._runner is None
    assert tools._bridge is None


async def test_closing_twice_is_safe():
    tools = toolset(port=free_port())
    await tools._connect()
    await tools.aclose()
    await tools.aclose()  # must not raise


# --- how it differs from websearch ----------------------------------------------------------


def test_the_two_web_toolsets_do_not_collide():
    """Both are about the web, so a shared tool name would make routing ambiguous."""
    from stark.tools.websearch import WebSearchTools

    browser = {s["function"]["name"] for s in BrowserTools().schemas()}
    websearch = {s["function"]["name"] for s in WebSearchTools().schemas()}
    assert browser & websearch == set()


def test_the_browser_tool_needs_no_extra():
    """aiohttp is already a core dependency, so there is nothing to install."""
    from stark.tools import CATALOG

    assert CATALOG["browser"].extras == ()
