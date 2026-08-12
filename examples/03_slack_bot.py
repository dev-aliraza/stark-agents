#!/usr/bin/env python3
"""The same agents, served to Slack instead of a terminal.

Only the `listener` argument changes — discovery, MCP wiring and delegation are identical.

Setup, once, at https://api.slack.com/apps:

1. Create an app, then under **Socket Mode** enable it and generate an app-level token
   with the `connections:write` scope. That is your SLACK_APP_TOKEN (`xapp-...`).
2. Under **OAuth & Permissions**, add the bot scopes `app_mentions:read`, `chat:write`,
   and `im:history`, then install the app. That is your SLACK_BOT_TOKEN (`xoxb-...`).
3. Under **Event Subscriptions**, subscribe to the bot events `app_mention` and
   `message.im`.
4. Invite the bot to a channel: `/invite @your-bot`.

Then:

    export SLACK_BOT_TOKEN=xoxb-...
    export SLACK_APP_TOKEN=xapp-...
    export ANTHROPIC_API_KEY=...
    pip install 'stark-agents[slack]'
    python examples/03_slack_bot.py

The bot answers @mentions in channels and direct messages, replying in-thread.

The answer is not streamed. You get a live progress message — one `:loading123:` line per
agent delegation and tool call, struck through with `:talabatdone:` as each finishes — and
then the finished answer as its own message. Add both custom emoji to your workspace, or
Slack renders the literal `:loading123:` text.

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
)
