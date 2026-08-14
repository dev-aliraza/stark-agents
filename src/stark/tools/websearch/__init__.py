"""Search the web and read what you find, as a native toolset.

An agent asks for it in its `tools:` block:

    tools:
      websearch:
        search_provider: brave
        search_key: ${BRAVE_SEARCH_API_KEY:-}

Search goes through an API — Brave or Serper — chosen by whichever key is set, with a
keyless DuckDuckGo fallback so it works with no signup. Pages are fetched over plain HTTP
and turned into text by `extraction`, which uses only the standard library. So the whole
toolset needs one dependency: `pip install 'stark-agents[websearch]'`.

There is no browser involved and nothing to install beyond that. A page that renders itself
with JavaScript comes back empty, and the tool says so.

Note the module is `extraction`, not `extract`, and `providers`, not `search`: re-exporting a
function with the same name as its module shadows the module, and `import x.extract as m`
then silently binds the function.
"""

from .extraction import Document, Link, extract
from .fetch import FetchError, FetchResult, fetch_html
from .providers import SearchError, SearchResult, choose_provider, search
from .tools import WEBSEARCH_TOOL_NAMES, WebSearchTools, schemas

__all__ = [
    "WebSearchTools",
    "WEBSEARCH_TOOL_NAMES",
    "schemas",
    "extract",
    "Document",
    "Link",
    "fetch_html",
    "FetchResult",
    "FetchError",
    "search",
    "SearchResult",
    "SearchError",
    "choose_provider",
]
