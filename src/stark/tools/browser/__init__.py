"""Drive a real Chrome, through the stark-browser extension.

    tools:
      browser:
        port: 8765

The extension connects *to* Stark, not the other way round — a browser service worker cannot
hold a listening socket. So declaring this toolset opens a local WebSocket server on first
use, and the extension's popup points at it.

What this buys over `websearch`: the user's own browser, with their sessions, running the
page's JavaScript, and able to click and type. What it costs: the extension has to be
installed and connected, and every call is a round trip through it.

The extension only ever touches tabs it opened itself. An agent gets its own tabs and cannot
see, read or click the user's — which is enforced in the extension, not here.
"""

from .bridge import BridgeError, Browser, BrowserBridge, acquire, release
from .tools import (
    BROWSER_TOOL_NAMES,
    VISION_TOOL_NAMES,
    BrowserTools,
    schemas,
    vision_schemas,
)

__all__ = [
    "BrowserTools",
    "BROWSER_TOOL_NAMES",
    "VISION_TOOL_NAMES",
    "schemas",
    "vision_schemas",
    "BrowserBridge",
    "BridgeError",
    "Browser",
    "acquire",
    "release",
]
