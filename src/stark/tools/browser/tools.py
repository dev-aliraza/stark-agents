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

BROWSER_TOOL_NAMES = (
    OPEN, TEXT, ELEMENTS, CLICK, FILL, PRESS, SCROLL, NAVIGATE, TABS, CLOSE,
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
}


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    key: {"type": kind, "description": text}
                    for key, (kind, text) in properties.items()
                },
                **({"required": required} if required else {}),
            },
        },
    }

_TAB = ("integer", "The tab id returned by browser_open.")


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
            "Press a key on whatever has focus — 'Enter' to submit a form, 'Escape' to "
            "dismiss something.",
            {"tabId": _TAB, "key": ("string", "A key name, such as 'Enter'.")},
            required=["tabId", "key"],
        ),
        _tool(
            SCROLL,
            "Scroll the page, for content that loads as you go.",
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
        self.host = str(settings.get("host") or DEFAULT_HOST).strip()
        self.port = _int(settings.get("port"), DEFAULT_PORT)
        self.token = str(settings.get("token") or "").strip()
        self.command_timeout = _float(settings.get("timeout"), DEFAULT_COMMAND_TIMEOUT)
        self.connect_timeout = _float(settings.get("connect_timeout"), DEFAULT_CONNECT_TIMEOUT)
        self._bridge = None

    # --- ToolSet ----------------------------------------------------------------------

    def schemas(self) -> list[dict[str, Any]]:
        return schemas()

    def owns(self, tool_name: str) -> bool:
        return tool_name in BROWSER_TOOL_NAMES

    async def aclose(self) -> None:
        """Let go of the shared bridge; the last one out stops the server."""
        if self._bridge is not None:
            await release(self._bridge)
            self._bridge = None

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        route = _ROUTES.get(tool_name)
        if route is None:
            return f"[error] unknown browser tool '{tool_name}'"

        try:
            params = _params(tool_name, arguments)
        except ValueError as exc:
            return f"[error] {exc}"

        try:
            bridge = await self._connect()
            result = await bridge.call(route, params)
        except BridgeError as exc:
            return f"[error] {exc}"
        except Exception as exc:  # pragma: no cover - unexpected transport failure
            logger.debug("Browser tool %s failed: %s", tool_name, exc)
            return f"[error] {tool_name} failed: {type(exc).__name__}: {exc}"

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
    if tool_name == NAVIGATE:
        url = str(arguments.get("url") or "").strip()
        if not url:
            raise ValueError("'url' is required")
        params["url"] = url
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


def _float(value: Any, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default
