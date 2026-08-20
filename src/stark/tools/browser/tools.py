"""The browser toolset: drive a real Chrome through the stark-browser extension.

Different from `websearch` in the one way that matters. `websearch` fetches over HTTP as
nobody in particular — no cookies, no session, no JavaScript. This drives a browser you are
already signed into, running the page's own scripts, in a tab the extension opened.

So the two are complements, not rivals:

    reading a public article   → websearch_open, one HTTP request, cheap
    a page behind your login   → browser_open, the real thing
    a page that needs clicking → browser_open, and only this

The loop for anything interactive is always the same three steps, because refs are
reassigned on every read:

    browser_elements(tab)              → [{ref: "ref_5", role: "input", name: "Email"}, …]
    browser_fill(tab, "ref_5", "…")
    browser_elements(tab)              → read again; the old refs are stale
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...logger import get_logger
from ...types import ToolImage, ToolResult
from .bridge import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_HOST,
    DEFAULT_PORT,
    BridgeError,
    acquire,
    release,
)

logger = get_logger("browser")

# The one coordinate space, for every model: 0-1000 across the width and 0-1000 down the
# height, whatever the image's pixel size.
#
# Models do not agree on what a coordinate means. Claude answers in the pixels of the image it
# saw; Gemini is trained to answer on a normalised 0-1000 grid regardless of the image. Adopting
# one family's convention leaves the other clicking somewhere plausible but wrong — off to one
# side, compressed towards a corner — and nothing about that looks like an error, so it never
# corrects itself. Declaring the space instead of inferring it removes the whole class of bug.
COORD_SPACE = 1000

OPEN = "browser_open"
TEXT = "browser_text"
ELEMENTS = "browser_elements"
CLICK = "browser_click"
FILL = "browser_fill"
PRESS = "browser_press"
SCROLL = "browser_scroll"
NAVIGATE = "browser_navigate"
TABS = "browser_tabs"
CLOSE = "browser_close"

# Vision. Offered only when the agent asks for it *and* its model can accept an image.
SCREENSHOT = "browser_screenshot"
CLICK_AT = "browser_click_at"
TYPE = "browser_type"
DRAG = "browser_drag"
FIND = "browser_find"
CLICK_TEXT = "browser_click_text"

VISION_TOOL_NAMES = (SCREENSHOT, CLICK_AT, TYPE, DRAG, FIND, CLICK_TEXT)
BROWSER_TOOL_NAMES = (
    OPEN, TEXT, ELEMENTS, CLICK, FILL, PRESS, SCROLL, NAVIGATE, TABS, CLOSE,
    *VISION_TOOL_NAMES,
)

# What each tool maps to on the extension side.
_ROUTES = {
    OPEN: "tabs.create",
    TEXT: "get_text",
    ELEMENTS: "read_page",
    CLICK: "click",
    FILL: "fill",
    PRESS: "key",
    SCROLL: "scroll",
    NAVIGATE: "navigate",
    TABS: "tabs.list",
    CLOSE: "tabs.close",
    SCREENSHOT: "screenshot",
    CLICK_AT: "click_at",
    TYPE: "type_text",
    DRAG: "drag",
    FIND: "find",
    CLICK_TEXT: "click_text",
}


def _property(kind: str, text: str) -> dict[str, Any]:
    """One parameter, declared so every provider can validate it.

    An `array` **must** say what it contains. Anthropic and OpenAI accept one that does not;
    Gemini refuses the whole function declaration, and LiteLLM papers over it by guessing
    `items: {"type": "object"}` — which is wrong for a list of strings, so the model then sends
    `[{}]` and the call is rejected on arrival. Every array here holds strings.
    """
    declared: dict[str, Any] = {"type": kind, "description": text}
    if kind == "array":
        declared["items"] = {"type": "string"}
    return declared


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    key: _property(kind, text) for key, (kind, text) in properties.items()
                },
                **({"required": required} if required else {}),
            },
        },
    }

_TAB = ("integer", "The tab id returned by browser_open.")


def vision_schemas() -> list[dict[str, Any]]:
    """The three tools that only make sense to a model that can see.

    Kept apart from the rest because they are opt-in twice over: the agent asks for `vision:
    true`, and its model has to accept images. A model offered `browser_click_at` with no way
    to look at a screenshot would be guessing at coordinates.
    """
    return [
        _tool(
            SCREENSHOT,
            "Look at the page. Returns an image of the tab's visible area, which you will "
            "see in the next message. Use this when the page has nothing worth reading in "
            "browser_elements — a canvas app, a chart, a custom widget — or to check what "
            "actually happened after an action. For ordinary pages browser_elements and "
            "browser_text are cheaper and more precise. "
            "Pass grid=true to draw a labelled 0-1000 grid over the page: read the coordinate "
            "off the gridlines rather than judging it by eye, which is worth doing before any "
            "click you are not confident about.",
            {
                "tabId": _TAB,
                "grid": ("boolean", "Overlay a labelled coordinate grid. Off by default."),
            },
            required=["tabId"],
        ),
        _tool(
            CLICK_AT,
            "Click a point on the most recent screenshot of this tab. Coordinates are "
            "0-1000 across the width and 0-1000 down the height — NOT pixels: (0, 0) is the "
            "top-left corner, (500, 500) the middle, (1000, 1000) the bottom-right, whatever "
            "size the image is. Take a screenshot first, and again after scrolling. "
            "Use button='right' to open a context menu — in an app like Google Docs that is "
            "how you duplicate a tab, insert a table column, or delete a row. Use clicks=2 to "
            "select a word or enter a cell, clicks=3 to select a whole line or paragraph. "
            "Use modifiers=['shift'] to extend a selection to this point: click the first "
            "cell of a range, scroll if you need to, then shift-click the last one and the "
            "whole span is selected — that is how you clear or fill many cells at once "
            "instead of visiting each.",
            {
                "tabId": _TAB,
                "x": ("integer", "0-1000 from the left edge. 500 is the middle."),
                "y": ("integer", "0-1000 from the top edge. 500 is the middle."),
                "button": ("string", "'left' (default) or 'right' for a context menu."),
                "clicks": ("integer", "1 (default), 2 to double-click, 3 to triple-click."),
                "modifiers": (
                    "array",
                    "Held while clicking: 'shift' extends a selection, 'mod' adds to one. "
                    "'mod' is Command on macOS and Control elsewhere.",
                ),
            },
            required=["tabId", "x", "y"],
        ),
        _tool(
            DRAG,
            "Drag from one point to another on the last screenshot — to reorder something, "
            "move a tab, or select a range by sweeping across it. Coordinates are 0-1000 on "
            "each axis, as for browser_click_at.",
            {
                "tabId": _TAB,
                "from_x": ("integer", "Start, 0-1000 from the left edge."),
                "from_y": ("integer", "Start, 0-1000 from the top edge."),
                "to_x": ("integer", "End, 0-1000 from the left edge."),
                "to_y": ("integer", "End, 0-1000 from the top edge."),
            },
            required=["tabId", "from_x", "from_y", "to_x", "to_y"],
        ),
        _tool(
            TYPE,
            "Type into whatever the last click focused. Click the field with "
            "browser_click_at first. Refuses password and other credential fields, the same "
            "as browser_fill.",
            {"tabId": _TAB, "text": ("string", "The text to type.")},
            required=["tabId", "text"],
        ),
        _tool(
            CLICK_TEXT,
            "Click the thing on screen with this text — a menu item, a button, a tab, a "
            "toolbar control. **Always prefer this over browser_click_at when the target has "
            "words on it.** It finds the element on the live page and clicks its centre, so "
            "there is no coordinate to estimate and nothing to go stale: it either hits the "
            "right thing or tells you it could not find it. Estimating a coordinate for "
            "'Insert column left' when 'Delete column' is 24 pixels below it is how documents "
            "get damaged. Refuses rather than guessing when several things share the text.",
            {
                "tabId": _TAB,
                "text": ("string", "The visible text, e.g. 'Insert column left'."),
                "button": ("string", "'left' (default) or 'right'."),
                "clicks": ("integer", "1 (default), 2 to double-click, 3 to triple-click."),
                "modifiers": ("array", "Held while clicking: 'shift', 'mod', 'alt'."),
            },
            required=["tabId", "text"],
        ),
        _tool(
            FIND,
            "Find where things with this text are, without clicking. Returns each match with "
            "its label and its coordinates in the usual 0-1000 space. Use it to check "
            "something exists, to read a menu before committing to an item, or to get exact "
            "coordinates instead of estimating them. Only sees real elements — content drawn "
            "on a canvas is invisible to it, and that is what screenshots are for.",
            {
                "tabId": _TAB,
                "text": ("string", "The text to look for. Partial matches count."),
                "limit": ("integer", "Maximum matches to return. Default 10."),
            },
            required=["tabId", "text"],
        ),
    ]


def schemas() -> list[dict[str, Any]]:
    """Tool schemas for the browser toolset."""
    return [
        _tool(
            OPEN,
            "Open a URL in a new browser tab and take control of it. Every other browser "
            "tool needs the tab id this returns. The browser is the user's own, so pages "
            "behind their login work — but the extension only ever touches tabs it opened, "
            "never the user's existing ones.",
            {
                "url": ("string", "The full http(s) URL."),
                "window": ("string", "Pass 'new' to open it in a separate window."),
            },
            required=["url"],
        ),
        _tool(
            TEXT,
            "Read the page as text — the tool for an article, a news story, a document. "
            "This is the rendered page, so JavaScript has already run.",
            {"tabId": _TAB, "max_chars": ("integer", "Cap on the returned text.")},
            required=["tabId"],
        ),
        _tool(
            ELEMENTS,
            "List what can be interacted with on the page: fields, buttons, links. Each gets "
            "a 'ref' to pass to browser_fill or browser_click. Refs are reassigned on every "
            "call and invalidated by navigation, so read again after anything changes.",
            {"tabId": _TAB, "limit": ("integer", "Maximum number of elements to return.")},
            required=["tabId"],
        ),
        _tool(
            FILL,
            "Type a value into one field, by its ref. Refuses password and other "
            "credential-shaped fields — ask the user to type those.",
            {
                "tabId": _TAB,
                "ref": ("string", "An element reference such as 'ref_5'."),
                "value": ("string", "The text to enter."),
            },
            required=["tabId", "ref", "value"],
        ),
        _tool(
            CLICK,
            "Click one element by its ref. The page often navigates as a result, so call "
            "browser_elements again afterwards rather than reusing old refs.",
            {"tabId": _TAB, "ref": ("string", "An element reference such as 'ref_3'.")},
            required=["tabId", "ref"],
        ),
        _tool(
            PRESS,
            "Press a key on whatever has focus, optionally with modifiers held. "
            "In an application with keyboard shortcuts this is far more reliable than hunting "
            "for a menu item by eye — a shortcut cannot be a few pixels off. Examples: "
            "key='a' modifiers=['mod'] to select all, key='ArrowDown' modifiers=['shift'] to "
            "extend a selection, key='Enter' to confirm, key='Escape' to dismiss a menu. "
            "Use 'mod' for the shortcut key and it is correct on every platform — Command on "
            "a Mac, Control elsewhere; 'ctrl' is accepted and means the same thing. Copy, "
            "cut, paste, select-all and undo are also invoked as real editor commands, which "
            "is what makes paste work at all.",
            {
                "tabId": _TAB,
                "key": (
                    "string",
                    "A single character, or Enter, Tab, Escape, Backspace, Delete, "
                    "ArrowUp/Down/Left/Right, Home, End, PageUp, PageDown, Space.",
                ),
                "modifiers": (
                    "array",
                    "Held while pressing. Use 'mod' for the shortcut key (Command on macOS, "
                    "Control elsewhere); also 'shift', 'alt', and 'control' for a literal "
                    "Control key.",
                ),
            },
            required=["tabId", "key"],
        ),
        _tool(
            SCROLL,
            "Scroll, for content further down the page. Finds whatever is actually scrolling "
            "— in an app like Google Docs that is a pane inside the page, not the page "
            "itself. Check 'scrolled' in the reply: it is the pixels that really moved, and "
            "0 means nothing did, so scrolling again will not help either. 'atEnd' tells you "
            "there is nothing below.",
            {"tabId": _TAB, "amount": ("integer", "Pixels; negative scrolls up.")},
            required=["tabId"],
        ),
        _tool(
            NAVIGATE,
            "Point an already-open tab at a different URL.",
            {"tabId": _TAB, "url": ("string", "The full http(s) URL.")},
            required=["tabId", "url"],
        ),
        _tool(
            TABS,
            "List the tabs this agent has open. The user's own tabs are not included and "
            "cannot be reached.",
            {},
        ),
        _tool(
            CLOSE,
            "Close a tab when you are finished with it. Worth doing: tabs left open are the "
            "user's to clear up.",
            {"tabId": _TAB},
            required=["tabId"],
        ),
    ]


class BrowserTools:
    """The browser toolset for one agent, from its `tools: browser:` block.

    Holds no browser of its own — it holds a claim on the shared bridge, which is what the
    extension connects to. Nothing is started until a tool is actually called, so an agent
    that never browses never opens a port.
    """

    def __init__(self, root: Path | str | None = None, settings: dict[str, Any] | None = None):
        settings = settings or {}
        self.root = Path(root) if root else None
        self.host = str(settings.get("host") or DEFAULT_HOST).strip()
        self.port = _int(settings.get("port"), DEFAULT_PORT)
        self.token = str(settings.get("token") or "").strip()
        self.command_timeout = _float(settings.get("timeout"), DEFAULT_COMMAND_TIMEOUT)
        self.connect_timeout = _float(settings.get("connect_timeout"), DEFAULT_CONNECT_TIMEOUT)
        # Off unless asked for. Screenshots are the most expensive thing this toolset can do,
        # and refs handle most pages better anyway.
        self.vision = _bool(settings.get("vision"), False)
        # Attach Chrome's debugger when a tab opens, rather than at the first screenshot.
        # For a vision-first agent that is every tab, and having the debugging bar appear
        # up front is less alarming than having it appear halfway through.
        self.attach_debugger = _bool(settings.get("attach_debugger"), False)
        if self.attach_debugger and not self.vision:
            logger.warning(
                "browser: attach_debugger is set but vision is not, so nothing will use the "
                "debugger. Add `vision: true` or drop attach_debugger."
            )
            self.attach_debugger = False
        # Narrate on the page: a cursor where the agent is acting, and a chip naming what it
        # is doing. On by default once the debugger is eager, since that is the mode where
        # someone is watching — but independent, so either can be had without the other.
        self.show_activity = _bool(settings.get("show_activity"), self.attach_debugger)
        # Where to keep screenshots. Unset means they are never written anywhere.
        self.screenshot_path = self._resolve_path(settings.get("screenshot_path"))
        self._saved = 0
        self._bridge = None

    def _resolve_path(self, value: Any) -> Path | None:
        """Where screenshots go, or None.

        A relative path resolves against the agent's own directory, which is the one place an
        agent already owns. An absolute path is honoured as given — this is authored config,
        like `shell`'s `cwd`, so naming a directory is the operator's decision to make.
        """
        text = str(value or "").strip()
        if not text:
            return None
        path = Path(text).expanduser()
        if not path.is_absolute() and self.root is not None:
            path = self.root / path
        return path

    # --- ToolSet ----------------------------------------------------------------------

    def schemas(self) -> list[dict[str, Any]]:
        return schemas() + (vision_schemas() if self.vision else [])

    def owns(self, tool_name: str) -> bool:
        return tool_name in BROWSER_TOOL_NAMES

    def needs_vision(self, tool_name: str) -> bool:
        """Which tools a text-only model must not be offered. Read by `ToolBox`."""
        return tool_name in VISION_TOOL_NAMES

    async def aclose(self) -> None:
        """Let go of the shared bridge; the last one out stops the server."""
        if self._bridge is not None:
            await release(self._bridge)
            self._bridge = None

    def _save_screenshot(self, result: dict[str, Any]) -> Path | None:
        """Write a screenshot to `screenshot_path`, if one was configured.

        Returns where it went, or None — both when saving is off and when it failed. A
        screenshot that could not be filed is not a reason to fail the tool call: the model
        still has the image, which is the part it actually needs. The warning goes to the log,
        where a person can act on it.
        """
        if self.screenshot_path is None:
            return None

        data_url = str(result.get("image") or "")
        if "," not in data_url:
            return None

        import base64
        from datetime import datetime

        self._saved += 1
        # Second-resolution timestamps collide within a single turn, so the counter is what
        # actually keeps names unique; the timestamp is there to make the directory readable.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = self.screenshot_path / (
            f"{stamp}-tab{result.get('tabId', 'x')}-{self._saved:03d}.png"
        )

        try:
            self.screenshot_path.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(base64.b64decode(data_url.partition(",")[2]))
        except Exception as exc:
            logger.warning("Could not save the screenshot to %s: %s", destination, exc)
            return None

        logger.info("Saved screenshot to %s", destination)
        return destination

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> str | ToolResult:
        route = _ROUTES.get(tool_name)
        if route is None:
            return f"[error] unknown browser tool '{tool_name}'"
        if tool_name in VISION_TOOL_NAMES and not self.vision:
            return (
                f"[error] {tool_name} needs vision, which is off for this agent. "
                f"Set `vision: true` under `browser:` in its tools block."
            )

        try:
            params = _params(tool_name, arguments)
        except ValueError as exc:
            return f"[error] {exc}"

        if tool_name == OPEN:
            if self.attach_debugger:
                params["debug"] = True
            if self.show_activity:
                params["hud"] = True

        try:
            bridge = await self._connect()
            result = await bridge.call(route, params)
        except BridgeError as exc:
            return f"[error] {exc}"
        except Exception as exc:  # pragma: no cover - unexpected transport failure
            logger.debug("Browser tool %s failed: %s", tool_name, exc)
            return f"[error] {tool_name} failed: {type(exc).__name__}: {exc}"

        if tool_name == SCREENSHOT:
            return _screenshot_result(result, self._save_screenshot(result))
        return json.dumps(_annotate(tool_name, result), indent=2, default=str)

    async def _connect(self):
        if self._bridge is None:
            self._bridge = await acquire(
                self.host,
                self.port,
                token=self.token,
                command_timeout=self.command_timeout,
                connect_timeout=self.connect_timeout,
            )
        return self._bridge


def _coordinate(arguments: dict[str, Any], axis: str) -> int:
    """One coordinate, checked against the declared space before it costs a round trip.

    A model using the wrong convention sends pixel values, which on any normal viewport
    overshoot 1000 — so refusing here turns a silent mis-click into an error that names the
    space and can be corrected on the next turn.
    """
    try:
        value = int(round(float(arguments[axis])))
    except (KeyError, TypeError, ValueError):
        raise ValueError(
            f"'{axis}' is required and must be a number between 0 and {COORD_SPACE}"
        ) from None

    if not 0 <= value <= COORD_SPACE:
        raise ValueError(
            f"'{axis}' is {value}, outside the coordinate space. Coordinates are "
            f"0-{COORD_SPACE} across the width and 0-{COORD_SPACE} down the height of the "
            f"screenshot — not pixels. The middle of the image is (500, 500)."
        )
    return value


def _modifier_names(raw: Any) -> list[str]:
    """Pull modifier names out of whatever shape a provider chose to send.

    A string, a list of strings, or — when a provider has been told the array holds objects —
    a list of single-valued dicts like `[{"name": "mod"}]`. Unwrapping those beats refusing
    them: the intent is unambiguous, and how a provider encodes a list of strings is not
    something the model chose or can correct.
    """
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []

    names: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            names.extend(str(value).strip() for value in entry.values() if str(value).strip())
        elif str(entry).strip():
            names.append(str(entry).strip())
    return [name.lower() for name in names]


def _modifiers(arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate the modifier list shared by `browser_press` and `browser_click_at`."""
    raw = arguments.get("modifiers") or []
    # "mod" is the platform-independent name for the shortcut key: Command on macOS, Control
    # elsewhere. The extension resolves it, because it is the only layer that knows the OS.
    known = {
        "mod", "cmdorctrl", "primary",
        "ctrl", "control", "meta", "cmd", "command", "alt", "shift",
    }
    cleaned = _modifier_names(raw)
    unknown = [entry for entry in cleaned if entry not in known]
    if unknown:
        raise ValueError(
            f"unknown modifier(s) {', '.join(unknown)}; use mod (the platform's shortcut "
            f"key), shift, alt, or control"
        )
    return {"modifiers": cleaned} if cleaned else {}


def _screenshot_result(result: dict[str, Any], saved_to: Path | None = None) -> str | ToolResult:
    """Turn the extension's data URL into an image the model will actually be shown.

    The text half says how big the image is, because that is the coordinate frame
    `browser_click_at` expects and the model has no other way to know it.
    """
    data_url = str(result.get("image") or "")
    if "," not in data_url:
        return "[error] the browser returned no image. Try again once the page has loaded."

    header, _, payload = data_url.partition(",")
    media_type = header[5:].split(";")[0] or "image/png"
    width, height = result.get("width"), result.get("height")

    summary = {
        "tabId": result.get("tabId"),
        "url": result.get("url"),
        "title": result.get("title"),
        "width": width,
        "height": height,
        **({"savedTo": str(saved_to)} if saved_to else {}),
        "coordinates": f"0-{COORD_SPACE} on each axis, not pixels",
        "note": (
            f"The image follows. To click a point on it, give browser_click_at coordinates "
            f"from 0 to {COORD_SPACE} on each axis — **not** pixels, and not the {width}x"
            f"{height} above. (0, 0) is the top-left corner, (500, 500) the middle, "
            f"({COORD_SPACE}, {COORD_SPACE}) the bottom-right. Work out roughly how far across "
            f"and how far down your target sits, as a fraction, and multiply by "
            f"{COORD_SPACE}. Only the visible area is shown — scroll and take another to see "
            f"further down, which also gives you a fresh frame."
        ),
    }
    return ToolResult(
        text=json.dumps(summary, indent=2, default=str),
        images=[
            ToolImage(
                data=payload,
                media_type=media_type,
                label=f"Screenshot of tab {result.get('tabId')} ({width}x{height}):",
            )
        ],
    )


def _params(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Translate a tool call into the extension's parameters, checking what it needs."""
    arguments = arguments or {}

    if tool_name == TABS:
        return {}

    if tool_name == OPEN:
        url = str(arguments.get("url") or "").strip()
        if not url:
            raise ValueError("'url' is required")
        params: dict[str, Any] = {"url": url}
        if str(arguments.get("window") or "").strip().lower() == "new":
            params["window"] = "new"
        return params

    # Everything else acts on a tab the agent already opened.
    tab_id = arguments.get("tabId")
    if not isinstance(tab_id, int):
        try:
            tab_id = int(tab_id)
        except (TypeError, ValueError):
            raise ValueError(
                "'tabId' is required and must be the number returned by browser_open"
            ) from None
    params = {"tabId": tab_id}

    if tool_name in (CLICK, FILL):
        ref = str(arguments.get("ref") or "").strip()
        if not ref:
            raise ValueError("'ref' is required; get one from browser_elements")
        params["ref"] = ref
    if tool_name == FILL:
        if arguments.get("value") is None:
            raise ValueError("'value' is required")
        params["value"] = str(arguments["value"])
    if tool_name == PRESS:
        key = str(arguments.get("key") or "").strip()
        if not key:
            raise ValueError("'key' is required, for example 'Enter'")
        params["key"] = key
        params.update(_modifiers(arguments))
    if tool_name == NAVIGATE:
        url = str(arguments.get("url") or "").strip()
        if not url:
            raise ValueError("'url' is required")
        params["url"] = url
    if tool_name == DRAG:
        for axis in ("from_x", "from_y", "to_x", "to_y"):
            params[axis] = _coordinate(arguments, axis)
    if tool_name == CLICK_AT:
        params.update(_modifiers(arguments))
        button = str(arguments.get("button") or "left").strip().lower()
        if button not in {"left", "right", "middle"}:
            raise ValueError(f"'button' must be 'left' or 'right', got '{button}'")
        if button != "left":
            params["button"] = button
        clicks = arguments.get("clicks")
        if clicks is not None:
            try:
                params["clicks"] = max(1, min(3, int(clicks)))
            except (TypeError, ValueError):
                raise ValueError("'clicks' must be 1, 2 or 3") from None
        for axis in ("x", "y"):
            params[axis] = _coordinate(arguments, axis)
    if tool_name in (CLICK_TEXT, FIND):
        text = str(arguments.get("text") or "").strip()
        if not text:
            raise ValueError("'text' is required — the words on the thing you want")
        params["text"] = text
        if tool_name == FIND and arguments.get("limit") is not None:
            params["limit"] = max(1, min(25, _int(arguments.get("limit"), 10)))
        if tool_name == CLICK_TEXT:
            params.update(_modifiers(arguments))
            button = str(arguments.get("button") or "left").strip().lower()
            if button not in {"left", "right", "middle"}:
                raise ValueError(f"'button' must be 'left' or 'right', got '{button}'")
            if button != "left":
                params["button"] = button
            if arguments.get("clicks") is not None:
                params["clicks"] = max(1, min(3, _int(arguments.get("clicks"), 1)))
    if tool_name == SCREENSHOT and _bool(arguments.get("grid"), False):
        params["grid"] = True
    if tool_name == TYPE:
        text = str(arguments.get("text") or "")
        if not text:
            raise ValueError("'text' is required")
        params["text"] = text
    if tool_name == SCROLL and arguments.get("amount") is not None:
        params["amount"] = _int(arguments.get("amount"), 600)
    if tool_name == TEXT and arguments.get("max_chars") is not None:
        params["max_chars"] = _int(arguments.get("max_chars"), 40000)
    if tool_name == ELEMENTS and arguments.get("limit") is not None:
        params["limit"] = _int(arguments.get("limit"), 60)

    return params


def _annotate(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Add the one thing the raw result does not say but the model needs to know."""
    if tool_name == ELEMENTS and result.get("elements") is not None:
        result = {
            **result,
            "note": (
                "These refs belong to this read only. After a click, a fill that changes the "
                "page, or any navigation, call browser_elements again before acting."
            ),
        }
    elif tool_name == TEXT and not str(result.get("text") or "").strip():
        result = {
            **result,
            "note": "The page returned no text. It may still be loading — try again, or scroll.",
        }
    return result


def _int(value: Any, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "on", "1"}


def _float(value: Any, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default
