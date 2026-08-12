#!/usr/bin/env python3
"""The smallest useful Stark program.

Discovers the agents in `examples/agents`, starts their MCP servers, and opens an
interactive CLI. Everything else in this folder is a variation on these three lines.

    export ANTHROPIC_API_KEY=...
    python examples/01_quickstart.py

Then try a query that needs two agents, so the orchestrator delegates in parallel:

    what are EMEA sales and is ATL-PRO-001 in stock?
"""

import os
import sys

import stark

# inventory-agent declares `command: ${PYTHON:-python3}` for its MCP server. Point that at
# the interpreter running this script, so the server starts on the environment that
# actually has the `mcp` package installed.
os.environ.setdefault("PYTHON", sys.executable)

stark.run(agents="examples/agents")
