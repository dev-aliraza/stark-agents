#!/usr/bin/env python3
"""Read and fill in pages in the user's own Chrome.

    export ANTHROPIC_API_KEY=...
    python examples/07_browser_tasks.py

Needs the **stark-browser** extension, which is a separate repo:

    1. chrome://extensions → Developer mode → Load unpacked → pick its `chrome/` folder.
    2. Open the extension's popup and Connect. The default address is ws://127.0.0.1:8765.

Until that is done every browser call fails with a message saying exactly that, so you will
not be left guessing.

Then try:

    Open https://news.ycombinator.com and tell me the top three stories.
    Open https://httpbin.org/forms/post and fill in the pizza order form.
    Open https://www.google.com/maps and tell me what you can see.

## Why this and not `websearch`

`websearch` fetches over HTTP as nobody in particular: no cookies, no session, no
JavaScript. It is cheaper and it is the right tool for a public article. This is the other
half — the user's real browser, signed in, running the page's own scripts, and able to click
and type. Keep both agents around and let the orchestrator pick.

## The connection runs backwards from what you would expect

A Chrome service worker cannot listen on a socket, so **the extension dials out and Stark
listens**. Declaring `tools: browser:` opens a local WebSocket server on first use — nothing
is bound at boot, so an agent that never browses never opens a port.

## The agent gets its own tabs, and only those

`browser_open` is the only way in. The extension owns the tab it creates and refuses every
command aimed at a tab it does not own, so the agent can act on exactly what it opened.
The tabs you have open are not listed, not readable, not clickable. Enforced in the
extension rather than here, which is where a boundary like that belongs.

So the agent cannot pick up a page you already have open — it opens its own tab at that URL.
Your cookies come with it, so a login carries over.

## Elements are addressed by ref, never by coordinate

    browser_elements(tab)          → [{ref: "ref_3", role: "input", name: "Full name"}, ...]
    browser_fill(tab, "ref_3", "Ada Lovelace")
    browser_elements(tab)          → read again; the old refs are stale

Clicking (x, y) breaks on a different window size, a different zoom, or an ad that shifts the
layout — and it fails by clicking something else, silently. Refs fail loudly instead.

## Vision: looking, not just reading

`browser-agent` has `vision: true`, so it can also take a screenshot and act on what it sees.
That is the only route into a page with no DOM worth reading — a canvas app like Google Docs
or Maps, a chart, a widget built from bare divs — and it is how the agent checks what an
action actually did.

    browser_screenshot(tab)          → an image, sent to the model in the next message
    browser_click_at(tab, 412, 388)  → a real click, at that point on that image
    browser_type(tab, "Ada")         → into whatever the click focused

It stays model-agnostic because of *how* the image travels, not because of a branch per
provider: one OpenAI-shaped `image_url` block that LiteLLM turns into Anthropic's `image`,
Gemini's `inline_data`, and nothing at all for OpenAI. And because a model that cannot accept
images is never offered the tools — `litellm.supports_vision` is checked at startup and the
three are withheld with a warning.

Two costs worth knowing before you turn it on:

- **Chrome shows a debugging bar** on the agent's tab while vision is in use. Screenshots and
  trusted clicks need the DevTools Protocol; the bar is the browser being honest about that.
  It is dropped after 90 seconds idle.
- **A screenshot is ~1,600 tokens, re-sent every turn.** Stark keeps the last two and stubs
  out the rest, which is roughly a 3x saving over a ten-step task.

Refs still beat pixels for anything `browser_elements` can see: picking from a list cannot
land on the wrong element, and a coordinate can — silently.

## The debugger only appears when vision is used

`browser-agent` reaches for vision as a fallback, so the debugger attaches the first time
something actually calls for a screenshot — and on a page `browser_elements` can read, that
never happens and no debugging bar appears. That is deliberate: an agent working from page
structure has no business raising it.

If you want the debugger attached to every tab from the moment it opens, that is
`examples/08_visual_browsing.py` and `vision-agent`, which sets `attach_debugger: true`.

## Two things it will not do

Type into a password field, and press the button on anything irreversible. The agent fills a
form and hands it back; submitting an order or an application is the user's call.
"""

import stark

stark.run(
    agents="examples/agents",
    listener="cli",
    # Both web agents take part, so the orchestrator has a real choice to make between
    # "fetch this page" and "work in this page".
    exclude_agents=[
        "draft-agent",
        "vision-agent",
        "sales-agent",
        "inventory-agent",
        "writer-agent",
        "ticket-opener",
        "answer-archiver",
        "ops-agent",
    ],
    instructions=(
        "You reach the web through two agents and neither is you. Send anything that needs "
        "a real browser — a page behind a login, a page that renders with JavaScript, "
        "anything that has to be clicked or typed into — to browser-agent. Send a plain "
        "public page or an open-ended search to web-agent, which is cheaper. "
        "Never press a button that commits something: if browser-agent reports a filled-in "
        "form, show the user what it entered and let them submit it themselves."
    ),
)
