#!/usr/bin/env python3
"""Every argument of `stark.run()`, put to use.

Shows the master system prompt that governs the orchestration loop, and
`exclude_agents` skipping a directory that is otherwise a valid agent.

    export ANTHROPIC_API_KEY=...
    python examples/02_custom_instructions.py

Ask for a briefing and watch it chain agents — figures and stock first, then the writer
agent turning them into prose:

    write me a one-paragraph briefing on EMEA performance and ATL-LITE-002 stock
"""

import os
import sys

import stark

# See 01_quickstart.py: pin inventory-agent's MCP server to this interpreter.
os.environ.setdefault("PYTHON", sys.executable)

INSTRUCTIONS = """\
You are the coordinator of a small commercial-operations team.

Your job is to answer the user's question completely, using your agents:

- Gather facts before you write anything. Sales figures and stock levels come from the
  data agents, never from your own knowledge.
- When a question needs several independent facts, ask for them all in one turn so the
  agents work in parallel.
- When the user wants prose rather than raw numbers, gather the facts first, then pass
  them to the writer agent in its `context` field.
- Finish by answering the user yourself in plain language. Never mention which agents you
  used or that you delegated at all.
- If the data does not support an answer, say so plainly instead of guessing.
"""

stark.run(
    agents="examples/agents",
    listener="cli",
    # draft-agent/AGENT.md is valid, but we do not want it in the roster yet.
    exclude_agents=["draft-agent"],
    instructions=INSTRUCTIONS,
)
