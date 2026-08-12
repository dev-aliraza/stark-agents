# Stark examples

Five runnable programs over one shared set of agents. Start with `05` if you have no API
key yet — it exercises the whole flow for free.

| Example | What it shows | Needs an API key |
| --- | --- | --- |
| [`01_quickstart.py`](01_quickstart.py) | The smallest useful program: `stark.run()` + CLI | yes |
| [`02_custom_instructions.py`](02_custom_instructions.py) | Master system prompt, `exclude_agents`, agent chaining | yes |
| [`03_slack_bot.py`](03_slack_bot.py) | The same agents served to Slack | yes |
| [`04_embed_programmatically.py`](04_embed_programmatically.py) | No listener: `Registry` + `Orchestrator` + a custom `ResponseSink` | yes |
| [`05_offline_walkthrough.py`](05_offline_walkthrough.py) | The full flow with a **fake model** — real scripts, real MCP, no cost | **no** |

## Run them

```bash
# From the repo root.
uv sync --extra dev --extra slack      # or: pip install -e '.[slack]'

# Free, no key needed — start here.
.venv/bin/python examples/05_offline_walkthrough.py

# The real thing.
export ANTHROPIC_API_KEY=...
.venv/bin/python examples/01_quickstart.py
```

> ⚠️ **A `.env` in your working directory is loaded automatically.** LiteLLM calls
> `load_dotenv()` when it is imported, so any variable in `./.env` — `ANTHROPIC_API_KEY`,
> `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` — is already set by the time Stark reads it. That is
> convenient, but it means `03_slack_bot.py` will connect to whatever workspace those
> tokens belong to without you exporting anything. Check your `.env` before running the
> Slack example.

Then ask something that needs two agents, so you can watch them run in parallel:

> what were EMEA sales in Q2, and is ATL-LITE-002 in stock?

## The example agents

```
examples/agents/
├── ticket-opener/        type: script — a triggerRule fires run(), no LLM at all
│   ├── AGENT.md
│   └── open_ticket.py
├── sales-agent/          runs a local script through workspace_run
│   ├── AGENT.md
│   └── query_sales.py
├── inventory-agent/      talks to a real MCP server over stdio
│   ├── AGENT.md
│   └── server.py
├── writer-agent/         the minimum viable agent — AGENT.md and nothing else
│   └── AGENT.md
├── draft-agent/          valid, but skipped via exclude_agents in example 02
│   └── AGENT.md
└── scratch/              no AGENT.md, so discovery skips it silently
    └── NOTES.md
```

The `=====` marker in a message is what fires `ticket-opener`, so try:

> ===== ArgoCD is down in prod =====

You'll see the script step run, its ticket posted as its own message, and then the
orchestrator answering with that ticket already in its context.

Between them they cover every discovery rule and both kinds of tool:

| Agent | Demonstrates |
| --- | --- |
| `ticket-opener` | A `type: script` agent: a `triggerRule` fires `open_ticket.py` with **no model involved**, `priority: 200` puts it ahead of the default band, and `send_output: true` posts its result to the user |
| `sales-agent` | An `AGENT.md` instructing the agent to run its own script; `workspace_run` executes it in a subprocess, sandboxed to the agent's directory |
| `inventory-agent` | An `mcp:` list with one enabled stdio server and one parked (`enable: false`) HTTP server, `${PYTHON:-python3}` env expansion, and `exclude:` hiding a destructive tool from the model |
| `writer-agent` | An agent that needs no tools, driven purely by its instructions |
| `draft-agent` | `exclude_agents` skipping a directory that would otherwise load |
| `scratch/` | A directory without `AGENT.md` being ignored rather than erroring |

### MCP config at a glance

`inventory-agent/AGENT.md` declares two servers and starts one:

```yaml
mcp:
  - name: warehouse            # started — enable is true
    enable: true
    command: ${PYTHON:-python3}
    args: ["server.py"]
    exclude: ["purge_warehouse"]

  - name: supplier-api         # skipped entirely — never spawned
    enable: false
    transport: streamable_http
    url: https://mcp.example.com/suppliers
```

Run example 05 and the startup log tells you which ones are live:

```
Loaded agent 'inventory-agent' (anthropic/claude-opus-5, effort=low, mcp=warehouse)
Agent 'inventory-agent': MCP server 'warehouse' ready with 2 tool(s)
```

Two tools, not three: `purge_warehouse` exists on the server but `exclude:` keeps it away
from the model.

### The `PYTHON` variable

`inventory-agent` starts its MCP server with `command: ${PYTHON:-python3}`. A bare
`python3` is usually the *system* interpreter, which does not have the `mcp` package — so
every example sets `PYTHON` to its own interpreter first:

```python
os.environ.setdefault("PYTHON", sys.executable)
```

If you run the CLI instead of a script, set it yourself:

```bash
PYTHON=$(pwd)/.venv/bin/python .venv/bin/stark --agents examples/agents
```

This is worth internalising for real agents too: an MCP server declared in frontmatter
runs as a subprocess, so it needs an interpreter (or binary) that has its own dependencies
installed. Its working directory is the agent's own folder, which is why
`args: ["server.py"]` resolves.

## What example 05 actually does

Only `LLMClient.complete` is replaced. Discovery, delegation, subprocess execution and MCP
all run for real:

```
orchestrator ──┬──▶ sales-agent      ──▶ runs query_sales.py      (real subprocess)
               └──▶ inventory-agent  ──▶ calls check_stock        (real MCP server)
                    │  both in parallel — one orchestrator turn, two tool calls
orchestrator ───────┴──▶ writer-agent ──▶ facts passed in via `context`
orchestrator ───────────▶ final answer
```

Expected output ends with:

```
orchestrator turns : 3
agents used        : inventory-agent, sales-agent, writer-agent
```

Because it needs no credentials and no network, it doubles as a smoke test for a new agent
folder: swap the path in `Registry.create(...)` and you will see whether your agents load,
whether their MCP servers come up, and which tools each one ends up with.

## Using the CLI instead

Every example has a CLI equivalent:

```bash
PYTHON=$(pwd)/.venv/bin/python .venv/bin/stark \
  --agents examples/agents \
  --exclude draft-agent \
  --instructions "You coordinate a commercial-ops team. Gather facts before answering." \
  --verbose
```

`python -m stark` works the same way. In the prompt, `/agents` lists the roster and
`/exit` quits.

## Configuring the orchestrator

The agents carry their own model settings in frontmatter. The orchestration loop's model
comes from the environment — see the table in the [main README](../README.md#configuring-the-orchestrator):

```bash
export STARK_MODEL=claude-opus-5
export STARK_EFFORT=high
```
