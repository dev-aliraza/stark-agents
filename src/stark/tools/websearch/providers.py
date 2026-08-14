"""Web search, behind a provider interface.

Driving google.com in a browser is the least reliable way to search: consent dialogs, bot
detection, layout that changes weekly, and it is against their terms. A search API returns
JSON, needs one key, and does not break. So the supported path is an API, chosen by
whichever key is present in the environment.

The DuckDuckGo fallback exists so the shipped example runs with no signup at all. It parses
an HTML page, which means it will break when that page changes — it reports that plainly
rather than pretending to have found nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

BRAVE = "brave"
SERPER = "serper"
DUCKDUCKGO = "duckduckgo"

BRAVE_KEY_ENV = "BRAVE_SEARCH_API_KEY"
SERPER_KEY_ENV = "SERPER_API_KEY"
PROVIDER_ENV = "STARK_SEARCH_PROVIDER"

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
SERPER_URL = "https://google.serper.dev/search"
DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"

DEFAULT_LIMIT = 10
MAX_LIMIT = 25


class SearchError(Exception):
    """The search could not be completed, with a reason worth showing the model."""


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""

    def as_payload(self) -> dict:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


def choose_provider(env: dict[str, str] | None = None) -> str:
    """Pick a provider: an explicit choice, else whichever key is configured."""
    env = env if env is not None else dict(os.environ)

    explicit = (env.get(PROVIDER_ENV) or "").strip().lower()
    if explicit:
        if explicit not in (BRAVE, SERPER, DUCKDUCKGO):
            raise SearchError(
                f"{PROVIDER_ENV}='{explicit}' is not a known provider; expected "
                f"{BRAVE}, {SERPER} or {DUCKDUCKGO}"
            )
        if explicit == BRAVE and not env.get(BRAVE_KEY_ENV):
            raise SearchError(f"{PROVIDER_ENV} is '{BRAVE}' but {BRAVE_KEY_ENV} is not set")
        if explicit == SERPER and not env.get(SERPER_KEY_ENV):
            raise SearchError(f"{PROVIDER_ENV} is '{SERPER}' but {SERPER_KEY_ENV} is not set")
        return explicit

    if env.get(BRAVE_KEY_ENV):
        return BRAVE
    if env.get(SERPER_KEY_ENV):
        return SERPER
    return DUCKDUCKGO


# --- response parsing (pure, so it can be tested without a network) -------------------


def parse_brave(payload: dict, limit: int) -> list[SearchResult]:
    results = ((payload or {}).get("web") or {}).get("results") or []
    return [
        SearchResult(
            title=_text(item.get("title")),
            url=_text(item.get("url")),
            snippet=_text(item.get("description")),
        )
        for item in results[:limit]
        if item.get("url")
    ]


def parse_serper(payload: dict, limit: int) -> list[SearchResult]:
    results = (payload or {}).get("organic") or []
    return [
        SearchResult(
            title=_text(item.get("title")),
            url=_text(item.get("link")),
            snippet=_text(item.get("snippet")),
        )
        for item in results[:limit]
        if item.get("link")
    ]


class _DuckDuckGoParser(HTMLParser):
    """Pulls result links and snippets out of the HTML endpoint's markup."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._in_title = False
        self._in_snippet = False
        self._title: list[str] = []
        self._snippet: list[str] = []
        self._url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()

        if tag == "a" and "result__a" in classes:
            self._flush()
            self._in_title = True
            self._url = _unwrap_redirect(values.get("href") or "")
        elif "result__snippet" in classes:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if self._in_title and tag == "a":
            self._in_title = False
        elif self._in_snippet and tag in ("a", "div", "td", "span"):
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title.append(data)
        elif self._in_snippet:
            self._snippet.append(data)

    def _flush(self) -> None:
        title = " ".join("".join(self._title).split())
        if title and self._url:
            self.results.append(
                SearchResult(
                    title=title,
                    url=self._url,
                    snippet=" ".join("".join(self._snippet).split()),
                )
            )
        self._title, self._snippet, self._url = [], [], ""

    def finish(self) -> list[SearchResult]:
        self._flush()
        return self.results


def parse_duckduckgo(html: str, limit: int) -> list[SearchResult]:
    parser = _DuckDuckGoParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup should yield fewer results, not an exception
        pass
    return parser.finish()[:limit]


def _unwrap_redirect(href: str) -> str:
    """DuckDuckGo wraps results as /l/?uddg=<encoded target>."""
    href = href.strip()
    if href.startswith("//"):
        href = f"https:{href}"
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return target[0]
    return href if parsed.scheme in ("http", "https") else ""


def _text(value) -> str:
    return " ".join(str(value or "").split())


# --- the call itself -------------------------------------------------------------------


async def search(
    query: str,
    limit: int = DEFAULT_LIMIT,
    client=None,
    env: dict[str, str] | None = None,
) -> tuple[str, list[SearchResult]]:
    """Run one search. Returns the provider used and its results."""
    import httpx

    from .fetch import build_client

    query = query.strip()
    if not query:
        raise SearchError("'query' is required")
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))

    env = env if env is not None else dict(os.environ)
    provider = choose_provider(env)

    owned = client is None
    client = client or build_client()
    try:
        if provider == BRAVE:
            response = await client.get(
                BRAVE_URL,
                params={"q": query, "count": limit},
                headers={"X-Subscription-Token": env[BRAVE_KEY_ENV], "Accept": "application/json"},
            )
            _raise_for_status(response, provider)
            results = parse_brave(response.json(), limit)
        elif provider == SERPER:
            response = await client.post(
                SERPER_URL,
                json={"q": query, "num": limit},
                headers={"X-API-KEY": env[SERPER_KEY_ENV], "Content-Type": "application/json"},
            )
            _raise_for_status(response, provider)
            results = parse_serper(response.json(), limit)
        else:
            from .fetch import PAGE_HEADERS

            response = await client.post(
                DUCKDUCKGO_URL, data={"q": query, "kl": "wt-wt"}, headers=PAGE_HEADERS
            )
            _raise_for_status(response, provider)
            results = parse_duckduckgo(response.text, limit)
    except httpx.HTTPError as exc:
        raise SearchError(f"{provider} search failed — {type(exc).__name__}: {exc}") from exc
    finally:
        if owned:
            await client.aclose()

    if not results:
        raise SearchError(
            f"{provider} returned no usable results for '{query}'."
            + (
                " The DuckDuckGo fallback parses HTML and breaks when that page changes; "
                f"set {BRAVE_KEY_ENV} or {SERPER_KEY_ENV} for a supported API."
                if provider == DUCKDUCKGO
                else ""
            )
        )
    return provider, results


def _raise_for_status(response, provider: str) -> None:
    if response.status_code == 429:
        raise SearchError(f"{provider} rate-limited the request (429); try again shortly")
    if response.status_code in (401, 403):
        raise SearchError(f"{provider} rejected the API key ({response.status_code})")
    if response.status_code >= 400:
        raise SearchError(f"{provider} returned HTTP {response.status_code}")
