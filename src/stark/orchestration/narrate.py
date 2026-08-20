"""Turning a tool call and its result into one short line a person can read.

The CLI already showed which tool an agent reached for. It did not show *what* the agent did
with it — `browser_click_at` told you a click happened, not that it landed on "Delete column",
and `browser_scroll` told you nothing about whether the page moved. Watching an agent work is
most of how you tell a stuck run from a slow one, so the arguments and the outcome are worth
the few characters.

Everything here is defensive about size. A tool result can be a whole page of text or a JSON
blob with a base64 image in it, and none of that belongs in a progress line.
"""

from __future__ import annotations

import json
from typing import Any

# Long enough to be useful, short enough to stay on one terminal line next to a label.
VALUE_CHARS = 40
LINE_CHARS = 110

# Never worth showing: the caller already knows which tab, and a credential should not be
# echoed to a terminal or a Slack channel even when a tool would have refused it.
SKIP_ARGS = frozenset({"tabId", "tab_id"})
SECRET_ARGS = frozenset({"password", "token", "secret", "api_key", "apikey"})

# The fields worth reporting from a result, in the order they read best.
RESULT_KEYS = (
    "clicked", "matched", "typed", "into", "pressed", "command",
    "scrolled", "atEnd", "scroller",
    "tabId", "url", "title", "closed", "navigated",
    "width", "height", "space", "grid",
    "filled", "length", "exitCode", "duration",
)


def _short(value: Any, limit: int = VALUE_CHARS) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def describe_call(tool_name: str, arguments: dict[str, Any]) -> str:
    """`browser_click_text "Insert column left"` — the call, as it would be read aloud."""
    if not isinstance(arguments, dict) or not arguments:
        return tool_name

    parts: list[str] = []
    for key, value in arguments.items():
        if key in SKIP_ARGS or value is None or value == "":
            continue
        if key in SECRET_ARGS:
            parts.append(f"{key}=…")
            continue
        if isinstance(value, str):
            parts.append(f'{key}="{_short(value)}"' if len(parts) or key != "text" else f'"{_short(value)}"')
        elif isinstance(value, (list, tuple)):
            parts.append(f"{key}={','.join(_short(item, 12) for item in value)}")
        else:
            parts.append(f"{key}={_short(value, 12)}")
        if len(parts) == 4:
            break

    return f"{tool_name} {' '.join(parts)}".strip() if parts else tool_name


def describe_result(result_text: str) -> str:
    """What came back, in a few words — or "" when there is nothing worth saying.

    An empty string means the caller should stay quiet rather than print a line that only
    says the tool finished, which the reader can already see.
    """
    text = (result_text or "").strip()
    if not text:
        return ""
    if text.startswith("[error]"):
        return _short(text, LINE_CHARS)

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        # Not JSON: one line of it, which covers shell output and page text.
        return _short(text.splitlines()[0], LINE_CHARS)

    if isinstance(parsed, list):
        return f"{len(parsed)} result(s)"
    if not isinstance(parsed, dict):
        return _short(text, LINE_CHARS)

    if isinstance(parsed.get("matches"), list):
        matches = parsed["matches"]
        first = matches[0].get("label") if matches and isinstance(matches[0], dict) else ""
        return f"{len(matches)} match(es)" + (f': "{_short(first)}"' if first else "")

    parts = [
        f"{key}={_short(parsed[key], 28)}"
        for key in RESULT_KEYS
        if key in parsed and parsed[key] not in (None, "", [])
    ]
    return _short(" ".join(parts), LINE_CHARS) if parts else ""
