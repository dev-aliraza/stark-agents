#!/usr/bin/env python3
"""Answer a research question from the live web.

    pip install 'stark-agents[websearch]'
    export ANTHROPIC_API_KEY=...
    python examples/06_web_research.py

The orchestrator delegates to `web-agent`, which searches, opens the most trustworthy
result, and reports what it read. Two tool calls, not a sequence of clicks:

    websearch_search("top 10 destinations in UAE")  → title, url, snippet for each result
    websearch_open(<chosen url>)                    → the page as readable text

Notice what is *not* here. There is no "open google.com", no typing into a search box, no
clicking the first result. `websearch_search` returns URLs, so following one is just opening
it. Transcribing a human's UI steps into tool calls is how a two-call task becomes fifteen.

There is no browser in this toolset at all — search goes through an API, and pages are
fetched over HTTP and turned into text by a stdlib extractor. A page that builds itself with
JavaScript comes back empty, and the agent is told to try another source.

Search works with no signup — a keyless DuckDuckGo fallback is used when no key is set. It
parses HTML and is best-effort; for anything you rely on, set BRAVE_SEARCH_API_KEY.

There is no browser here — a page that builds itself with JavaScript comes back empty,
and the agent is told to try another source.

    pip install 'stark-agents[websearch]'
"""

import stark

stark.run(
    agents="examples/agents",
    listener="cli",
    # Only the web agent takes part, so the orchestrator has one obvious place to route.
    exclude_agents=[
        "draft-agent",
        "browser-agent",
        "sales-agent",
        "inventory-agent",
        "writer-agent",
        "ticket-opener",
        "answer-archiver",
        "ops-agent",
    ],
    instructions=(
        "You are a research assistant. Delegate anything that needs current information to "
        "web-agent — you cannot reach the web yourself, and your own knowledge has a cutoff. "
        "Report what it found, keep the source URL in your answer, and say plainly when the "
        "sources disagree or when nothing useful was found."
    ),
    config={
        "orchestrator": {
            "tools": {
                "shell": {}
            }
        }
    }
)
