"""Search, fetching and the tool surface — all offline.

Every HTTP call goes through `httpx.MockTransport`, so the suite needs no network and no
API keys.
"""

from __future__ import annotations

import json

import pytest

from stark.tools.websearch.fetch import FetchError, MAX_BYTES, check_url, fetch_html
from stark.tools.websearch.providers import (
    BRAVE,
    BRAVE_KEY_ENV,
    DUCKDUCKGO,
    PROVIDER_ENV,
    SERPER,
    SERPER_KEY_ENV,
    SearchError,
    choose_provider,
    parse_brave,
    parse_duckduckgo,
    parse_serper,
    search,
)

httpx = pytest.importorskip("httpx", reason="the websearch tool needs the [websearch] extra")


def client_returning(handler):
    """An httpx client whose requests never leave the process."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def html_response(body: str, status: int = 200, content_type: str = "text/html"):
    return httpx.Response(status, text=body, headers={"content-type": content_type})


# --- URL guards ------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com", "javascript:alert(1)"])
def test_only_http_urls_are_allowed(url):
    with pytest.raises(FetchError, match="only http and https"):
        check_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/admin",
        "http://127.0.0.1:8080/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
    ],
)
def test_private_and_local_targets_are_refused(url):
    """A model can be handed any URL, including one it read off a page."""
    with pytest.raises(FetchError, match="not a public address"):
        check_url(url)


def test_the_metadata_endpoint_error_names_the_escape_hatch():
    with pytest.raises(FetchError, match="STARK_WEBSEARCH_ALLOW_PRIVATE"):
        check_url("http://169.254.169.254/")


def test_private_targets_are_allowed_when_asked_for():
    assert check_url("http://127.0.0.1:9000/health", allow_private=True)


def test_a_url_with_no_host_is_refused():
    with pytest.raises(FetchError, match="no host"):
        check_url("http://")


def test_an_unresolvable_host_says_so():
    with pytest.raises(FetchError, match="cannot resolve host"):
        check_url("http://nonexistent.invalid./")


# --- fetching --------------------------------------------------------------------------


async def test_fetch_returns_the_markup():
    async def handler(request):
        assert request.headers["user-agent"].startswith("Mozilla/5.0")
        return html_response("<title>Hi</title><p>Body.</p>")

    result = await fetch_html("https://example.com/page", client=client_returning(handler))

    assert result.status == 200
    assert "Body." in result.html
    assert result.url == "https://example.com/page"


async def test_a_redirect_target_is_checked_too():
    """Otherwise the private-address guard is one redirect away from useless."""

    async def handler(request):
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secrets"})
        return html_response("should never be reached")

    with pytest.raises(FetchError, match="not a public address"):
        await fetch_html("https://example.com/start", client=client_returning(handler))


async def test_binary_content_is_refused_rather_than_decoded():
    async def handler(request):
        return httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})

    with pytest.raises(FetchError, match="not a web page"):
        await fetch_html("https://example.com/doc.pdf", client=client_returning(handler))


async def test_an_oversized_body_is_capped():
    async def handler(request):
        return html_response("x" * (MAX_BYTES + 5_000))

    result = await fetch_html("https://example.com/big", client=client_returning(handler))
    assert len(result.html) <= MAX_BYTES


async def test_a_transport_failure_becomes_a_fetch_error():
    async def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(FetchError, match="ConnectError"):
        await fetch_html("https://example.com", client=client_returning(handler))


async def test_an_http_error_status_is_still_returned():
    """A 404 page often explains itself, and the model can read the status."""

    async def handler(request):
        return html_response("<p>Not found</p>", status=404)

    result = await fetch_html("https://example.com/gone", client=client_returning(handler))
    assert result.status == 404
    assert "Not found" in result.html


# --- provider selection ----------------------------------------------------------------


def test_brave_is_used_when_its_key_is_set():
    assert choose_provider({BRAVE_KEY_ENV: "k"}) == BRAVE


def test_serper_is_used_when_only_its_key_is_set():
    assert choose_provider({SERPER_KEY_ENV: "k"}) == SERPER


def test_brave_wins_when_both_keys_are_set():
    assert choose_provider({BRAVE_KEY_ENV: "a", SERPER_KEY_ENV: "b"}) == BRAVE


def test_duckduckgo_is_the_keyless_fallback():
    """So the shipped example runs with no signup."""
    assert choose_provider({}) == DUCKDUCKGO


def test_an_explicit_provider_overrides_the_keys():
    assert choose_provider({PROVIDER_ENV: "duckduckgo", BRAVE_KEY_ENV: "k"}) == DUCKDUCKGO


def test_an_unknown_provider_is_rejected():
    with pytest.raises(SearchError, match="not a known provider"):
        choose_provider({PROVIDER_ENV: "google"})


def test_choosing_a_provider_without_its_key_is_rejected():
    with pytest.raises(SearchError, match=f"{BRAVE_KEY_ENV} is not set"):
        choose_provider({PROVIDER_ENV: "brave"})


# --- response parsing ------------------------------------------------------------------


BRAVE_PAYLOAD = {
    "web": {
        "results": [
            {"title": "Top 10 UAE", "url": "https://a.example/uae", "description": "A guide."},
            {"title": "Also UAE", "url": "https://b.example/uae", "description": "Another."},
            {"title": "No URL", "description": "Dropped."},
        ]
    }
}


def test_brave_results_are_parsed():
    results = parse_brave(BRAVE_PAYLOAD, limit=10)
    assert [item.url for item in results] == ["https://a.example/uae", "https://b.example/uae"]
    assert results[0].title == "Top 10 UAE"
    assert results[0].snippet == "A guide."


def test_brave_respects_the_limit():
    assert len(parse_brave(BRAVE_PAYLOAD, limit=1)) == 1


def test_brave_handles_an_empty_payload():
    assert parse_brave({}, limit=10) == []
    assert parse_brave({"web": {}}, limit=10) == []


def test_serper_results_are_parsed():
    payload = {"organic": [{"title": "T", "link": "https://c.example", "snippet": "S"}]}
    results = parse_serper(payload, limit=10)
    assert results[0].url == "https://c.example"
    assert results[0].snippet == "S"


DUCKDUCKGO_HTML = """
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fvisitdubai.com%2Fuae">
    Top 10 destinations in the UAE
  </a>
  <a class="result__snippet">From the official tourism board.</a>
</div>
<div class="result">
  <a class="result__a" href="https://plain.example/uae">A direct link</a>
  <a class="result__snippet">No redirect wrapper.</a>
</div>
"""


def test_duckduckgo_results_are_parsed_and_unwrapped():
    results = parse_duckduckgo(DUCKDUCKGO_HTML, limit=10)

    assert len(results) == 2
    # The /l/?uddg= wrapper is unwrapped to the real target.
    assert results[0].url == "https://visitdubai.com/uae"
    assert results[0].title == "Top 10 destinations in the UAE"
    assert results[0].snippet == "From the official tourism board."
    assert results[1].url == "https://plain.example/uae"


def test_duckduckgo_returns_nothing_when_the_markup_changes():
    """It parses HTML, so it will break one day — it must break quietly."""
    assert parse_duckduckgo("<div class='totally-new-layout'>x</div>", limit=10) == []


# --- the search call -------------------------------------------------------------------


async def test_brave_search_end_to_end():
    seen = {}

    async def handler(request):
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("x-subscription-token")
        return httpx.Response(200, json=BRAVE_PAYLOAD)

    provider, results = await search(
        "top 10 destinations in UAE",
        limit=2,
        client=client_returning(handler),
        env={BRAVE_KEY_ENV: "secret-key"},
    )

    assert provider == BRAVE
    assert len(results) == 2
    assert seen["token"] == "secret-key"
    assert "top+10+destinations+in+UAE" in seen["url"] or "top%2010" in seen["url"]


async def test_serper_search_posts_json():
    async def handler(request):
        assert request.headers.get("x-api-key") == "k"
        assert json.loads(request.content)["q"] == "uae"
        return httpx.Response(200, json={"organic": [{"title": "T", "link": "https://x.example"}]})

    provider, results = await search(
        "uae", client=client_returning(handler), env={SERPER_KEY_ENV: "k"}
    )
    assert (provider, results[0].url) == (SERPER, "https://x.example")


async def test_an_empty_query_is_rejected():
    with pytest.raises(SearchError, match="'query' is required"):
        await search("   ")


async def test_a_rate_limit_is_reported_plainly():
    async def handler(request):
        return httpx.Response(429, json={})

    with pytest.raises(SearchError, match="rate-limited"):
        await search("x", client=client_returning(handler), env={BRAVE_KEY_ENV: "k"})


async def test_a_rejected_key_is_reported_plainly():
    async def handler(request):
        return httpx.Response(401, json={})

    with pytest.raises(SearchError, match="rejected the API key"):
        await search("x", client=client_returning(handler), env={BRAVE_KEY_ENV: "k"})


async def test_no_results_from_the_fallback_suggests_an_api_key():
    async def handler(request):
        return html_response("<div>nothing recognisable</div>")

    with pytest.raises(SearchError, match=BRAVE_KEY_ENV):
        await search("x", client=client_returning(handler), env={})


async def test_the_limit_is_clamped():
    captured = {}

    async def handler(request):
        captured["count"] = dict(request.url.params).get("count")
        return httpx.Response(200, json=BRAVE_PAYLOAD)

    await search("x", limit=999, client=client_returning(handler), env={BRAVE_KEY_ENV: "k"})
    assert captured["count"] == "25"  # MAX_LIMIT


# --- the toolset surface ----------------------------------------------------------------


def toolset(**settings):
    from stark.tools.websearch import WebSearchTools

    return WebSearchTools(None, settings)


def test_the_toolset_offers_exactly_two_tools():
    """Find pages, and read them. There is no browser here to drive."""
    from stark.tools.websearch import WEBSEARCH_TOOL_NAMES

    names = {schema["function"]["name"] for schema in toolset().schemas()}

    assert names == set(WEBSEARCH_TOOL_NAMES)
    assert names == {"websearch_search", "websearch_open"}


def test_the_toolset_claims_only_its_own_tools():
    tools = toolset()
    assert tools.owns("websearch_open") is True
    assert tools.owns("file_read") is False


def test_open_says_it_is_not_a_browser():
    """The model needs to know why an empty page is empty."""
    schema = next(s for s in toolset().schemas() if s["function"]["name"] == "websearch_open")
    assert "not a browser" in schema["function"]["description"]


async def test_search_returns_results_as_data(monkeypatch):
    from stark.tools.websearch import tools as websearch_tools
    from stark.tools.websearch.providers import SearchResult

    async def fake_search(query, limit, env=None):
        return "brave", [SearchResult(title="T", url="https://a.example", snippet="S")]

    monkeypatch.setattr(websearch_tools, "search", fake_search)
    result = json.loads(await toolset().call("websearch_search", {"query": "uae"}))

    assert result["provider"] == "brave"
    assert result["results"] == [{"title": "T", "url": "https://a.example", "snippet": "S"}]


async def test_open_returns_readable_text(monkeypatch):
    from stark.tools.websearch import tools as websearch_tools
    from stark.tools.websearch.fetch import FetchResult

    async def fake_fetch(url, allow_private=False):
        return FetchResult(
            url=url,
            status=200,
            html="<title>UAE</title><main><p>Burj Khalifa is 828 metres.</p></main>",
        )

    monkeypatch.setattr(websearch_tools, "fetch_html", fake_fetch)
    result = json.loads(await toolset().call("websearch_open", {"url": "https://example.com/uae"}))

    assert result["title"] == "UAE"
    assert "828 metres" in result["text"]
    assert result["status"] == 200


async def test_a_javascript_page_says_why_it_is_empty(monkeypatch):
    """No browser to fall back to, so the advice is to find another source."""
    from stark.tools.websearch import tools as websearch_tools
    from stark.tools.websearch.fetch import FetchResult

    async def fake_fetch(url, allow_private=False):
        return FetchResult(url=url, status=200, html='<body><div id="root"></div></body>')

    monkeypatch.setattr(websearch_tools, "fetch_html", fake_fetch)
    result = json.loads(await toolset().call("websearch_open", {"url": "https://example.com/app"}))

    assert "JavaScript" in result["note"]
    assert "another source" in result["note"]


async def test_a_refused_url_comes_back_as_an_error_string():
    """A tool that raises gives the model a traceback; one that returns can be recovered from."""
    result = await toolset().call("websearch_open", {"url": "http://169.254.169.254/"})

    assert result.startswith("[error]")
    assert "not a public address" in result


async def test_a_search_failure_comes_back_as_an_error_string(monkeypatch):
    from stark.tools.websearch import tools as websearch_tools

    async def fake_search(query, limit, env=None):
        raise SearchError("brave rate-limited the request (429); try again shortly")

    monkeypatch.setattr(websearch_tools, "search", fake_search)
    assert "rate-limited" in await toolset().call("websearch_search", {"query": "uae"})


async def test_an_unknown_tool_is_reported():
    assert "unknown websearch tool" in await toolset().call("websearch_nope", {})


async def test_closing_is_free_because_nothing_is_held():
    tools = toolset()
    await tools.aclose()  # stateless: no browser, no session, no connection pool
    assert tools.owns("websearch_search")


# --- per-agent provider settings -----------------------------------------------------------


def test_private_addresses_are_refused_unless_configured():
    assert toolset().allow_private is False
    assert toolset(allow_private=True).allow_private is True


def test_a_configured_provider_overrides_the_environment(monkeypatch):
    from stark.tools.websearch.providers import BRAVE, BRAVE_KEY_ENV, PROVIDER_ENV

    monkeypatch.delenv(BRAVE_KEY_ENV, raising=False)
    env = toolset(search_provider="brave", search_key="k").search_env()

    assert env[PROVIDER_ENV] == BRAVE
    assert env[BRAVE_KEY_ENV] == "k"


def test_a_serper_key_lands_on_the_serper_variable():
    from stark.tools.websearch.providers import SERPER_KEY_ENV

    env = toolset(search_provider="serper", search_key="k").search_env()
    assert env[SERPER_KEY_ENV] == "k"


def test_no_provider_setting_leaves_the_environment_alone(monkeypatch):
    from stark.tools.websearch.providers import PROVIDER_ENV

    monkeypatch.delenv(PROVIDER_ENV, raising=False)
    assert PROVIDER_ENV not in toolset().search_env()


def test_two_agents_can_search_with_different_providers():
    """The reason the provider is a setting rather than only an env var."""
    from stark.tools.websearch.providers import PROVIDER_ENV

    assert toolset(search_provider="brave").search_env()[PROVIDER_ENV] == "brave"
    assert toolset(search_provider="duckduckgo").search_env()[PROVIDER_ENV] == "duckduckgo"


# --- there is no browser here ---------------------------------------------------------------


def test_playwright_is_gone():
    """The toolset is HTTP only, so nothing should reach for a driver."""
    import importlib

    module = importlib.import_module("stark.tools.websearch")
    assert not hasattr(module, "BrowserSession")
    assert "playwright" not in str(module.__all__).lower()


def test_the_catalog_names_only_the_websearch_extra():
    from stark.tools import CATALOG

    assert CATALOG["websearch"].extras == ("websearch",)
    assert "playwright" not in " ".join(CATALOG["websearch"].extras)
