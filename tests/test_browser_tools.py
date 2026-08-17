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

from stark.tools.browser import (
    BROWSER_TOOL_NAMES,
    VISION_TOOL_NAMES,
    BridgeError,
    BrowserTools,
)
from stark.tools.browser.bridge import BrowserBridge, acquire, release
from stark.types import ToolResult

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

    # The vision three are owned but not offered until asked for, so this is a strict subset.
    assert names == set(BROWSER_TOOL_NAMES) - set(VISION_TOOL_NAMES)
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


# --- vision -------------------------------------------------------------------------------

PIXEL = "iVBORw0KGgoAAAANSUhEUg=="


@contextlib.asynccontextmanager
async def connected(running: BrowserBridge, **settings):
    """A toolset wired to an already-running bridge.

    `BrowserTools` reaches its bridge through `acquire`, which would otherwise build a second
    one and fail to bind the port the fixture already holds. Seeding the shared registry is
    what a real run does anyway — the first toolset to need a port puts it there.
    """
    from stark.tools.browser import bridge as bridge_module

    key = (running.host, running.port)
    bridge_module._BRIDGES[key] = running
    tools = toolset(port=running.port, **settings)
    try:
        yield tools
    finally:
        await tools.aclose()
        bridge_module._BRIDGES.pop(key, None)


def test_vision_tools_are_withheld_until_asked_for():
    """Screenshots are the most expensive thing here, so they are opt-in."""
    names = {schema["function"]["name"] for schema in toolset().schemas()}
    assert not (set(VISION_TOOL_NAMES) & names)


def test_vision_true_offers_them():
    names = {schema["function"]["name"] for schema in toolset(vision=True).schemas()}
    assert set(VISION_TOOL_NAMES) <= names


def test_vision_accepts_a_yaml_style_string():
    """`vision: "true"` from a hand-edited AGENT.md must not read as off."""
    assert toolset(vision="true").vision is True
    assert toolset(vision="no").vision is False


def test_the_toolset_marks_which_tools_need_a_seeing_model():
    tools = toolset(vision=True)
    assert tools.needs_vision("browser_screenshot") is True
    assert tools.needs_vision("browser_elements") is False


async def test_calling_a_vision_tool_with_vision_off_says_how_to_turn_it_on():
    result = await toolset().call("browser_screenshot", {"tabId": 1})
    assert "vision: true" in result


async def test_a_screenshot_comes_back_as_an_image_the_model_will_see(bridge):
    reply = {
        "tabId": 42,
        "url": "https://example.com",
        "title": "Example",
        "image": f"data:image/png;base64,{PIXEL}",
        "width": 1400,
        "height": 875,
    }
    async with FakeExtension(endpoint(bridge), {"screenshot": reply}):
        async with connected(bridge, vision=True) as tools:
            result = await tools.call("browser_screenshot", {"tabId": 42})

    assert isinstance(result, ToolResult)
    assert len(result.images) == 1
    image = result.images[0]
    # The base64 payload only — the data: prefix is rebuilt per provider by LiteLLM.
    assert image.data == PIXEL
    assert image.media_type == "image/png"
    assert "42" in image.label

    # The text half must state the coordinate frame; nothing else tells the model what
    # numbers browser_click_at will accept.
    assert '"width": 1400' in result.text and '"height": 875' in result.text


async def test_a_screenshot_with_no_image_is_an_error_not_an_empty_picture(bridge):
    async with FakeExtension(endpoint(bridge), {"screenshot": {"tabId": 42, "image": ""}}):
        async with connected(bridge, vision=True) as tools:
            result = await tools.call("browser_screenshot", {"tabId": 42})

    assert isinstance(result, str) and "no image" in result


async def test_click_at_requires_coordinates():
    tools = toolset(vision=True)
    result = await tools.call("browser_click_at", {"tabId": 1})
    assert "'x' is required" in result


async def test_click_at_rejects_non_numeric_coordinates():
    tools = toolset(vision=True)
    assert "must be a number" in await tools.call(
        "browser_click_at", {"tabId": 1, "x": "left-ish", "y": 10}
    )


async def test_click_at_passes_rounded_pixels_through(bridge):
    async with FakeExtension(endpoint(bridge), {"click_at": {"clickedAt": {}}}) as extension:
        async with connected(bridge, vision=True) as tools:
            await tools.call("browser_click_at", {"tabId": 42, "x": 411.6, "y": 388.2})

    assert extension.received[0]["params"] == {"tabId": 42, "x": 412, "y": 388}


async def test_typing_requires_text():
    assert "'text' is required" in await toolset(vision=True).call("browser_type", {"tabId": 1})


async def test_the_extensions_credential_refusal_reaches_the_model(bridge):
    """The guard lives in the extension; this checks the wording survives the trip."""
    refusal = (
        "the focused field looks like a credential field, which this extension will not "
        "fill. Ask the user to type it."
    )
    async with FakeExtension(endpoint(bridge), {"type_text": refusal}):
        async with connected(bridge, vision=True) as tools:
            result = await tools.call("browser_type", {"tabId": 42, "text": "hunter2"})

    assert "credential field" in result


async def test_opening_a_tab_asks_for_the_debugger_when_eager(bridge):
    async with FakeExtension(endpoint(bridge), {"tabs.create": {"tabId": 7}}) as extension:
        async with connected(bridge, vision=True, attach_debugger=True) as tools:
            await tools.call("browser_open", {"url": "https://example.com"})

    assert extension.received[0]["params"]["debug"] is True


async def test_opening_a_tab_does_not_ask_for_it_by_default(bridge):
    """Lazy is the default: an agent reading the DOM should not raise a debugging bar."""
    async with FakeExtension(endpoint(bridge), {"tabs.create": {"tabId": 7}}) as extension:
        async with connected(bridge, vision=True) as tools:
            await tools.call("browser_open", {"url": "https://example.com"})

    assert "debug" not in extension.received[0]["params"]


def test_eager_attachment_without_vision_is_refused_and_explained(caplog):
    """Nothing would ever use it, so the setting is a mistake worth naming."""
    with caplog.at_level("WARNING"):
        tools = toolset(attach_debugger=True)

    assert tools.attach_debugger is False
    assert "vision: true" in caplog.text


async def test_only_browser_open_carries_the_debug_flag(bridge):
    """It attaches the tab; sending it on every command would be noise."""
    async with FakeExtension(endpoint(bridge), {"read_page": {}}) as extension:
        async with connected(bridge, vision=True, attach_debugger=True) as tools:
            await tools.call("browser_elements", {"tabId": 7})

    assert "debug" not in extension.received[0]["params"]


# --- narrating on the page --------------------------------------------------------------


async def test_opening_a_tab_turns_on_the_overlay_when_configured(bridge):
    async with FakeExtension(endpoint(bridge), {"tabs.create": {"tabId": 7}}) as extension:
        async with connected(bridge, vision=True, show_activity=True) as tools:
            await tools.call("browser_open", {"url": "https://example.com"})

    assert extension.received[0]["params"]["hud"] is True


async def test_the_overlay_follows_the_debugger_by_default():
    """The eager-debugger mode is the one where somebody is watching the tab."""
    assert toolset(vision=True, attach_debugger=True).show_activity is True
    assert toolset(vision=True).show_activity is False


async def test_the_overlay_can_be_turned_off_independently():
    tools = toolset(vision=True, attach_debugger=True, show_activity=False)
    assert (tools.attach_debugger, tools.show_activity) == (True, False)


async def test_the_overlay_is_not_requested_by_default(bridge):
    async with FakeExtension(endpoint(bridge), {"tabs.create": {"tabId": 7}}) as extension:
        async with connected(bridge, vision=True) as tools:
            await tools.call("browser_open", {"url": "https://example.com"})

    assert "hud" not in extension.received[0]["params"]


# --- saving screenshots -------------------------------------------------------------------


def screenshot_reply(tab_id: int = 42) -> dict:
    return {
        "tabId": tab_id,
        "url": "https://example.com",
        "image": f"data:image/png;base64,{PIXEL}",
        "width": 1400,
        "height": 875,
    }


def test_no_screenshot_path_means_nothing_is_written():
    assert toolset(vision=True).screenshot_path is None


def test_a_relative_path_resolves_under_the_agents_own_directory(tmp_path):
    tools = BrowserTools(tmp_path, {"vision": True, "screenshot_path": "shots"})
    assert tools.screenshot_path == tmp_path / "shots"


def test_an_absolute_path_is_honoured_as_given(tmp_path):
    """Authored config, like shell's `cwd` — naming a directory is the operator's call."""
    tools = BrowserTools(tmp_path, {"vision": True, "screenshot_path": str(tmp_path / "e")})
    assert tools.screenshot_path == tmp_path / "e"


async def test_a_screenshot_is_written_and_the_path_reported(bridge, tmp_path):
    target = tmp_path / "shots"
    async with FakeExtension(endpoint(bridge), {"screenshot": screenshot_reply()}):
        async with connected(bridge, vision=True, screenshot_path=str(target)) as tools:
            result = await tools.call("browser_screenshot", {"tabId": 42})

    written = list(target.glob("*.png"))
    assert len(written) == 1
    # The bytes on disk are the image itself, not the data URL wrapper.
    import base64
    assert written[0].read_bytes() == base64.b64decode(PIXEL)
    # The model is told where it went, so it can refer to the file afterwards.
    assert str(written[0]) in result.text


async def test_the_directory_is_created_if_it_does_not_exist(bridge, tmp_path):
    target = tmp_path / "deep" / "nested" / "shots"
    async with FakeExtension(endpoint(bridge), {"screenshot": screenshot_reply()}):
        async with connected(bridge, vision=True, screenshot_path=str(target)) as tools:
            await tools.call("browser_screenshot", {"tabId": 42})

    assert len(list(target.glob("*.png"))) == 1


async def test_screenshots_in_one_turn_do_not_overwrite_each_other(bridge, tmp_path):
    """Timestamps are second-resolution, so the counter is what keeps names unique."""
    target = tmp_path / "shots"
    async with FakeExtension(endpoint(bridge), {"screenshot": screenshot_reply()}):
        async with connected(bridge, vision=True, screenshot_path=str(target)) as tools:
            for _ in range(3):
                await tools.call("browser_screenshot", {"tabId": 42})

    assert len(list(target.glob("*.png"))) == 3


async def test_an_unwritable_path_does_not_fail_the_tool_call(bridge, tmp_path, caplog):
    """The model still has the image, which is the part it needs."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("in the way", encoding="utf-8")

    async with FakeExtension(endpoint(bridge), {"screenshot": screenshot_reply()}):
        async with connected(bridge, vision=True, screenshot_path=str(blocker)) as tools:
            with caplog.at_level("WARNING"):
                result = await tools.call("browser_screenshot", {"tabId": 42})

    assert isinstance(result, ToolResult) and len(result.images) == 1
    assert "Could not save the screenshot" in caplog.text
    assert "savedTo" not in result.text


async def test_nothing_is_written_when_saving_is_off(bridge, tmp_path):
    async with FakeExtension(endpoint(bridge), {"screenshot": screenshot_reply()}):
        async with connected(bridge, vision=True) as tools:
            result = await tools.call("browser_screenshot", {"tabId": 42})

    assert list(tmp_path.iterdir()) == []
    assert "savedTo" not in result.text


# --- the input primitives a real application needs -----------------------------------------


async def test_a_right_click_reaches_the_extension(bridge):
    """Context menus are how Docs duplicates a tab or inserts a column."""
    async with FakeExtension(endpoint(bridge), {"click_at": {}}) as extension:
        async with connected(bridge, vision=True) as tools:
            await tools.call(
                "browser_click_at", {"tabId": 1, "x": 10, "y": 20, "button": "right"}
            )

    assert extension.received[0]["params"]["button"] == "right"


async def test_a_left_click_does_not_carry_a_button(bridge):
    """The default stays the default — no need to spend tokens restating it."""
    async with FakeExtension(endpoint(bridge), {"click_at": {}}) as extension:
        async with connected(bridge, vision=True) as tools:
            await tools.call("browser_click_at", {"tabId": 1, "x": 10, "y": 20})

    assert "button" not in extension.received[0]["params"]


async def test_an_unknown_button_is_refused():
    result = await toolset(vision=True).call(
        "browser_click_at", {"tabId": 1, "x": 1, "y": 1, "button": "sideways"}
    )
    assert "'button' must be" in result


async def test_click_counts_are_clamped_to_a_triple_click():
    from stark.tools.browser.tools import _params

    assert _params("browser_click_at", {"tabId": 1, "x": 1, "y": 1, "clicks": 9})["clicks"] == 3


async def test_modifiers_reach_the_extension(bridge):
    async with FakeExtension(endpoint(bridge), {"key": {}}) as extension:
        async with connected(bridge, vision=True) as tools:
            await tools.call(
                "browser_press", {"tabId": 1, "key": "a", "modifiers": ["ctrl"]}
            )

    assert extension.received[0]["params"]["modifiers"] == ["ctrl"]


async def test_a_single_modifier_string_is_accepted():
    from stark.tools.browser.tools import _params

    params = _params("browser_press", {"tabId": 1, "key": "End", "modifiers": "ctrl"})
    assert params["modifiers"] == ["ctrl"]


async def test_an_unknown_modifier_is_refused_by_name():
    result = await toolset(vision=True).call(
        "browser_press", {"tabId": 1, "key": "a", "modifiers": ["hyper"]}
    )
    assert "hyper" in result
    # The advice points at the platform-independent name, not at ctrl — which is wrong on a Mac.
    assert "mod" in result and "shift" in result


async def test_the_platform_independent_modifier_is_accepted():
    """`ctrl` is Control on macOS, where the shortcut key is Command.

    The extension resolves `mod` per platform, because it is the only layer that knows the OS.
    """
    from stark.tools.browser.tools import _params

    for name in ("mod", "cmdorctrl", "primary"):
        assert _params("browser_press", {"tabId": 1, "key": "v", "modifiers": [name]})[
            "modifiers"
        ] == [name]


async def test_mod_reaches_the_extension_unresolved(bridge):
    """Stark must not guess the platform — the browser is the one that knows."""
    async with FakeExtension(endpoint(bridge), {"key": {}}) as extension:
        async with connected(bridge, vision=True) as tools:
            await tools.call("browser_press", {"tabId": 1, "key": "v", "modifiers": ["mod"]})

    assert extension.received[0]["params"]["modifiers"] == ["mod"]


async def test_a_plain_key_press_carries_no_modifiers():
    from stark.tools.browser.tools import _params

    assert "modifiers" not in _params("browser_press", {"tabId": 1, "key": "Enter"})


async def test_dragging_needs_all_four_coordinates():
    result = await toolset(vision=True).call("browser_drag", {"tabId": 1, "from_x": 5})
    assert "'from_y' is required" in result


async def test_a_drag_reaches_the_extension(bridge):
    async with FakeExtension(endpoint(bridge), {"drag": {}}) as extension:
        async with connected(bridge, vision=True) as tools:
            await tools.call(
                "browser_drag",
                {"tabId": 1, "from_x": 5, "from_y": 6, "to_x": 700, "to_y": 80},
            )

    assert extension.received[0]["params"] == {
        "tabId": 1, "from_x": 5, "from_y": 6, "to_x": 700, "to_y": 80,
    }


def test_dragging_is_a_vision_tool():
    """It addresses points on a screenshot, so it is useless without one."""
    assert "browser_drag" in VISION_TOOL_NAMES
    assert toolset(vision=True).needs_vision("browser_drag") is True
    assert "browser_drag" not in {s["function"]["name"] for s in toolset().schemas()}


async def test_shift_click_reaches_the_extension(bridge):
    """The range-selection primitive: click the first cell, shift-click the last, act once."""
    async with FakeExtension(endpoint(bridge), {"click_at": {}}) as extension:
        async with connected(bridge, vision=True) as tools:
            await tools.call(
                "browser_click_at",
                {"tabId": 1, "x": 10, "y": 20, "modifiers": ["shift"]},
            )

    assert extension.received[0]["params"]["modifiers"] == ["shift"]


async def test_click_modifiers_are_validated_like_key_modifiers():
    result = await toolset(vision=True).call(
        "browser_click_at", {"tabId": 1, "x": 1, "y": 1, "modifiers": ["hyper"]}
    )
    assert "hyper" in result and "shift" in result


async def test_a_plain_click_carries_no_modifiers():
    from stark.tools.browser.tools import _params

    assert "modifiers" not in _params("browser_click_at", {"tabId": 1, "x": 1, "y": 1})


async def test_click_modifiers_compose_with_button_and_clicks():
    from stark.tools.browser.tools import _params

    params = _params(
        "browser_click_at",
        {"tabId": 1, "x": 1, "y": 1, "button": "right", "clicks": 2, "modifiers": "shift"},
    )
    assert params["modifiers"] == ["shift"]
    assert params["button"] == "right" and params["clicks"] == 2
