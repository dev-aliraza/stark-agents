"""The websearch toolset: find pages, and read them.

Two tools, and they answer a research question between them:

    websearch_search("top 10 destinations in UAE")   → [{title, url, snippet}, ...]
    websearch_open(<the most trustworthy url>)       → readable text

Then the model summarises from text it already has. **Summarising is not a tool** — tools
fetch, the model reasons.

There is no browser here. Pages are fetched over plain HTTP and turned into readable text
with the standard library, so this needs nothing but httpx: no browser binary, no driver, no
window. A page that renders its content with JavaScript will come back empty, and the tool
says so rather than pretending otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...logger import get_logger
from .extraction import DEFAULT_MAX_CHARS, extract
from .fetch import FetchError, fetch_html
from .providers import DEFAULT_LIMIT, SearchError, search

logger = get_logger("websearch")

SEARCH = "websearch_search"
OPEN = "websearch_open"

WEBSEARCH_TOOL_NAMES = (SEARCH, OPEN)

PROVIDER_SETTING = "search_provider"
KEY_SETTING = "search_key"


def schemas() -> list[dict[str, Any]]:
    """Tool schemas for the websearch toolset."""
    return [
        {
            "type": "function",
            "function": {
                "name": SEARCH,
                "description": (
                    "Search the web and return the results as data: title, URL and snippet "
                    "for each. Use this instead of opening a search engine and typing into "
                    "it — the results include each page's URL, so following one is "
                    "websearch_open, not a click."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to search for, as you would type it.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "How many results to return (1-25).",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": OPEN,
                "description": (
                    "Fetch a page and return its readable text, ready to summarise or "
                    "quote. This is an HTTP request, not a browser: a page that builds "
                    "itself with JavaScript comes back with no text, and the reply says so."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The full http(s) URL.",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "Cap on the returned text.",
                        },
                    },
                    "required": ["url"],
                },
            },
        },
    ]


class WebSearchTools:
    """The websearch toolset for one agent, from its `tools: websearch:` block.

    Stateless: every call is one HTTP request, so there is nothing to keep warm and nothing
    to shut down. One instance per agent all the same, because the search provider and key
    are per-agent settings.
    """

    def __init__(self, root: Path | str | None = None, settings: dict[str, Any] | None = None):
        settings = settings or {}
        self.allow_private = _flag(settings.get("allow_private"), False)
        self.provider = str(settings.get(PROVIDER_SETTING) or "").strip().lower()
        self.search_key = str(settings.get(KEY_SETTING) or "").strip()

    # --- ToolSet ----------------------------------------------------------------------

    def schemas(self) -> list[dict[str, Any]]:
        return schemas()

    def owns(self, tool_name: str) -> bool:
        return tool_name in WEBSEARCH_TOOL_NAMES

    async def aclose(self) -> None:
        """Nothing to release: each call opens and closes its own connection."""

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        try:
            if tool_name == SEARCH:
                payload = await self._search(
                    str(arguments.get("query") or ""),
                    arguments.get("limit") or DEFAULT_LIMIT,
                )
            elif tool_name == OPEN:
                payload = await self._open(
                    str(arguments.get("url") or ""),
                    _int(arguments.get("max_chars"), DEFAULT_MAX_CHARS),
                )
            else:
                return f"[error] unknown websearch tool '{tool_name}'"
        except (FetchError, SearchError) as exc:
            return f"[error] {exc}"
        except Exception as exc:  # pragma: no cover - unexpected transport failure
            logger.debug("Websearch tool %s failed: %s", tool_name, exc)
            return f"[error] {tool_name} failed: {type(exc).__name__}: {exc}"
        return _as_json(payload)

    # --- the work ----------------------------------------------------------------------

    def search_env(self) -> dict[str, str]:
        """Provider selection: this agent's settings, falling back to the environment."""
        import os

        from .providers import BRAVE_KEY_ENV, PROVIDER_ENV, SERPER, SERPER_KEY_ENV

        env = dict(os.environ)
        if self.provider:
            env[PROVIDER_ENV] = self.provider
        if self.search_key:
            env[SERPER_KEY_ENV if self.provider == SERPER else BRAVE_KEY_ENV] = self.search_key
        return env

    async def _search(self, query: str, limit) -> dict[str, Any]:
        provider, results = await search(query, limit, env=self.search_env())
        return {
            "query": query,
            "provider": provider,
            "results": [item.as_payload() for item in results],
        }

    async def _open(self, url: str, max_chars: int) -> dict[str, Any]:
        result = await fetch_html(url, allow_private=self.allow_private)
        document = extract(result.html, url=result.url, max_chars=max_chars)
        payload = document.as_payload()
        payload["status"] = result.status
        if not document.text.strip():
            payload["note"] = (
                "No readable text was found. The page probably builds itself with "
                "JavaScript, which this tool cannot run. Try another source."
            )
        return payload


def _flag(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _int(value: Any, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _as_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, default=str)
