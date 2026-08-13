#!/usr/bin/env python3
"""The same agents, served to Slack instead of a terminal.

Only the `listener` argument changes — discovery, MCP wiring and delegation are identical.

Setup, once, at https://api.slack.com/apps:

1. Create an app, then under **Socket Mode** enable it and generate an app-level token
   with the `connections:write` scope. That is your SLACK_APP_TOKEN (`xapp-...`).
2. Under **OAuth & Permissions**, add the bot scopes `app_mentions:read`, `chat:write`,
   and `im:history`, then install the app. That is your SLACK_BOT_TOKEN (`xoxb-...`).
3. Under **Event Subscriptions**, subscribe to the bot events `app_mention` and
   `message.im` — the same names this file passes to `config["slack"]["events"]`.
4. Invite the bot to a channel: `/invite @your-bot`.

Then:

    export SLACK_BOT_TOKEN=xoxb-...
    export SLACK_APP_TOKEN=xapp-...
    export ANTHROPIC_API_KEY=...
    pip install 'stark-agents[slack]'
    python examples/03_slack_bot.py

`config["slack"]["events"]` decides what the bot answers. Omit it entirely and only
`app_mention` is handled — a bot that speaks when named and not otherwise. This example adds
DMs, and public-channel messages carrying the `=====` escalation marker, which is what
`examples/agents/ticket-opener` looks for.

Each event's value is either `True` (answer all of them) or a filter in the same expression
language as an agent's `triggerRule`. A message that does not match is not answered at all —
no progress message, nothing posted. Enabling `app_mention` and `message.channels` together
is safe: a channel mention arrives as both events and the duplicate is dropped.

Note that `message.channels` needs the `channels:history` scope on top of the three above,
and messages from other bots are ignored unless you also pass `allow_bots: True`. Startup
logs exactly what it is listening for and names any scope you are missing.

The answer is not streamed. You get a live progress message — one `:hourglass:` line per
agent delegation and tool call, struck through with `:white_check_mark:` as each finishes —
and then the finished answer as its own message.

The `config` argument below also overrides those icons. All three defaults are built-in Slack
emoji, so they work anywhere; a custom shortcode must exist in your workspace first, or
Slack shows the literal `:name:` text.

Both tokens are checked before any MCP server starts, so a missing one fails fast.
"""

import os
import sys

import stark

# See 01_quickstart.py: pin inventory-agent's MCP server to this interpreter.
os.environ.setdefault("PYTHON", sys.executable)

stark.run(
    agents="examples/agents",
    listener="slack",
    exclude_agents=["draft-agent"],
    instructions=(
        "You are a commercial-operations assistant in Slack. Gather facts from your "
        "agents before answering. Keep replies short enough to read on a phone — a few "
        "sentences, or a short list when there are several figures. Use Slack mrkdwn "
        "(*bold*, `code`), never Markdown headings."
    ),
    config={
        "slack": {
            # Drop `events` to answer mentions only, which is the default.
            "events": {
                "app_mention": True,
                #"message.im": True,
                # Needs channels:history, and only fires on the escalation marker.
                #"message.channels": 'text.contains("=====")'
            },
            "allow_bots": True,
            # Drop these three to keep :hourglass:, :white_check_mark: and :x:.
            "running_emoji": ":hourglass_flowing_sand:",
            "done_emoji": ":white_check_mark:",
            "failed_emoji": ":x:",
        }
    },
)
