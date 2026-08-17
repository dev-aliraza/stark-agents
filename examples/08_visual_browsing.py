#!/usr/bin/env python3
"""Browse by looking at the screen, with Chrome's debugger attached from the start.

    export ANTHROPIC_API_KEY=...
    python examples/08_visual_browsing.py

Needs the **stark-browser** extension loaded and connected — see `07_browser_tasks.py`, or
the extension's own README.

Then try something with no DOM worth reading:

    Open https://www.google.com/maps and tell me what you can see.
    Open https://www.openstreetmap.org and zoom into central Dubai.
    Open https://excalidraw.com and draw a rectangle.

## How this differs from example 07

`07` uses `browser-agent`, which works from the page's structure — `browser_elements` gives it
a list of things with refs, and it clicks them by name. Vision is available there, but it is
the fallback, so the debugger only attaches the first time something calls for a screenshot.
On an ordinary page that never happens, and no debugging bar ever appears.

`08` loads **only** `vision-agent`, which works the other way round: it looks first and
clicks coordinates it reads off the image. Nothing else is registered, so every request goes
down the visual path and you are never left wondering which agent picked the work up. Its
tools block sets

    vision: true
    attach_debugger: true

and the second is what you are here for. Every tab it opens gets Chrome's debugger attached
immediately, so the yellow **"Stark Browser started debugging this browser"** bar is up for
the whole session rather than appearing partway through — which reads as though something
changed when nothing did.

## The failure mode to know about

The commonest way this goes wrong is not in the agent — it is in the **brief the orchestrator
writes**. A general-purpose model handed a complex document task will reach for reconnaissance
first: *"describe the structure of the document so I understand it… do not change anything
yet, just report what you see."* That reads like diligence. For this agent it is fatal: it
spends its whole budget scrolling, changes nothing, and the run looks like a scrolling bug.

No instruction inside `vision-agent` can override it, because it arrives as the task itself —
the agent is being obedient, not stupid. So the orchestrator here is told explicitly never to
invent a discovery step, and `vision-agent` in turn bounds any reporting task to a single
screenshot.

## Give it one finishable thing

The failure mode worth knowing about: this agent works from pictures, so an open-ended task
("look at this document", "tell me about this page") gives it no way to know when it is
finished — and it keeps screenshotting. The fix is in what you ask for. `add a line reading
"Reviewed by Ali" at the end of this doc` finishes; `read this doc` does not.

Its instructions push hard the same way — do the task, do not survey the page, a loop is a
failure rather than persistence — and its toolset is cut down to the nine tools that make
sense from a screenshot. `browser_text` and `browser_elements` are deliberately withheld: on a
canvas app they return a toolbar and nothing else, which reads as "keep looking".

## You can watch it work

`vision-agent` also sets `show_activity: true`, so the page itself narrates: a cursor ring
where the agent is clicking, and a chip in the corner reading *Reading what is on the page*,
*Clicking (412, 388)*, *Typing…*. A tab that scrolls and clicks on its own with no explanation
is unsettling, and a click that landed on the wrong element looks exactly like one that did
nothing at all — this makes both legible.

It is taken down before every screenshot, so the model gets the page rather than a picture of
Stark's own cursor. It is drawn in a shadow root with `pointer-events: none`, so the page
cannot restyle it and it cannot swallow the agent's own clicks. And it can never fail a
command: on a page the overlay cannot be drawn on, the click still happens.

## And keep what it saw

`screenshot_path: screenshots` writes every screenshot to
`examples/agents/vision-agent/screenshots/` as a timestamped PNG. Useful for seeing what the
agent actually looked at when its answer is wrong. Drop the line and nothing is written —
it is off by default, because most runs do not want a directory filling up.

## Why the debugger is needed at all

Two things extension APIs cannot do:

- **Screenshot the tab you name.** `chrome.tabs.captureVisibleTab` photographs whatever is
  visible in a *window*, so on a background tab it returns a picture of one of yours — the
  exact thing the ownership model exists to prevent. `Page.captureScreenshot` addresses the
  tab, and takes a scale, so Chrome does the downsizing.
- **Produce input the page believes.** Events synthesised inside the page carry
  `isTrusted: false`. Plenty of sites do not care; file pickers, native drag-and-drop and a
  good deal of anti-automation do.

It is attached only to tabs the agent opened, and dropped after 90 seconds idle, when the tab
closes, or the moment you pause the extension from its popup.

## Coordinates go stale, and are refused rather than landed

Only the visible area is captured, so scrolling moves everything. The scroll offset and
viewport size travel with each screenshot and are re-checked on every click:

    the page has scrolled or resized since that screenshot, so those coordinates no longer
    point at what you saw. Take another screenshot and use its coordinates.

That refusal is the whole reason coordinate clicking is safe enough to ship. A stale ref is
refused because it points at nothing; a stale coordinate would still point at *something*,
which is worse — you would get a confident click on the wrong element and no error.

## It still costs more than reading

A screenshot is roughly 1,600 tokens and every tool result is re-sent on every later turn, so
Stark keeps the last two and stubs out the rest. Even so, a visual session costs several times
a structural one.

This example spends that on everything, deliberately — it exists to show the visual path, not
to route around it. In a real setup you would keep all three: `web-agent` for a public article
(example 06), `browser-agent` for an ordinary interactive page (example 07), and this one only
for what neither can see.
"""

import stark

stark.run(
    agents="examples/agents",
    listener="cli",
    # vision-agent is the only one left, so every web request goes to it and you are always
    # watching the visual path rather than wondering which agent picked the work up.
    exclude_agents=[
        "draft-agent",
        "sales-agent",
        "inventory-agent",
        "writer-agent",
        "ticket-opener",
        "answer-archiver",
        "ops-agent",
        "web-agent",
        "browser-agent",
    ],
    instructions=(
        "You reach the web through vision-agent and not by yourself. It opens a tab in the "
        "user's own Chrome, looks at the page, and clicks what it sees.\n\n"
        "**Never delegate a reconnaissance step.** Do not ask it to describe, survey, "
        "inspect, summarise, or 'report back what you see', and never say 'do not change "
        "anything yet'. You do not need to understand the page to have work done on it — "
        "vision-agent is looking at it and you are not. A discovery step you invented costs "
        "dozens of screenshots and changes nothing, and it is the single most common way "
        "this goes wrong.\n\n"
        "Delegate a *reporting* task only when the user themselves asked a question about "
        "the page. Never as a preliminary to doing work.\n\n"
        "The user's steps are already concrete. Pass each one through as the action it is, "
        "in the user's own words, and let vision-agent find what it needs on the page. "
        "Never add a step the user did not ask for. If the request has several steps, "
        "delegate them one at a time and check each came back before sending the next.\n\n"
        "Report what it found. Never press a button that commits something: if it reports a "
        "filled-in form or a pending action, show the user what it saw and let them confirm."
    ),
)
