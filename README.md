# Stark Agents

A lightweight Python ADK for discovering and orchestrating multi-agent workflows. Define
each agent as a Markdown file, point Stark at the folder, and it handles discovery, MCP
tool wiring, delegation, and the listener that takes user input.

```python
import stark

stark.run()
```

That single call scans `./agents`, starts each agent's MCP servers, opens an interactive
CLI, and routes every query through an orchestration loop that delegates to whichever
agents are relevant — in parallel when the sub-tasks are independent.

## Features

- 📁 **Agents are folders, not code** — an `AGENT.md` with YAML frontmatter is a complete agent.
- 🔌 **Native MCP** — declare stdio or streamable-HTTP servers in frontmatter; they start once at boot and stay warm.
- 🧠 **Model agnostic** — LiteLLM underneath, so any of its 100+ providers works. Anthropic is the first-class default.
- 🎧 **Pluggable listeners** — an interactive CLI or a Slack Socket Mode bot, same orchestration behind both.
- ⚡ **Parallel delegation** — independent sub-tasks fan out concurrently; so do the tool calls inside each agent.
- 🛠️ **Built-in workspace tools** — every agent can list, read, and run the scripts in its own directory, sandboxed to it.
- 🧱 **Per-agent budgets** — each agent carries its own model, effort, iteration cap, and token cap.

## Installation

```bash
pip install stark-agents
```

For the Slack listener:

```bash
pip install 'stark-agents[slack]'
```

Requires Python 3.10+.

## Quick start

### 1. Create an agent

```
agents/
  ├── research-agent/
  │   ├── AGENT.md          # metadata + instructions
  │   └── find_research.py  # a script the agent can run
  └── example-agent/
      └── AGENT.md
```

`agents/research-agent/AGENT.md`:

```markdown
---
name: research-agent
description: Researches a topic and returns findings with sources. Give it one clear research question.
provider: anthropic
model: claude-opus-5
effort: medium
max_iterations: 20
max_output_tokens: 8192
---

# Instructions

1. Run `find_research.py` with the research question as its argument whenever you are
   asked to research a topic.
2. If it returns nothing relevant, say so rather than inventing an answer.
3. Report each finding with its source name.
```

The frontmatter is the contract; everything below it becomes the agent's system prompt.

### 2. Run it

```python
import stark

stark.run(
    agents="./agents",
    listener="cli",
    exclude_agents=["draft-agent"],
    instructions="You coordinate a research team. Delegate, then answer the user directly.",
)
```

Or from the terminal:

```bash
export ANTHROPIC_API_KEY=...
stark --agents ./agents --listener cli
```

## Examples

[`examples/`](examples/README.md) has five runnable programs over one shared agent folder:
the quickstart, custom instructions with `exclude_agents`, a Slack bot, direct embedding
with a custom `ResponseSink`, and an offline walkthrough.

Start with the offline one — it needs **no API key** and no network, because only the model
is faked. Real discovery, a real subprocess, and a real MCP server all take part:

```bash
python examples/05_offline_walkthrough.py
```

The `agents/` folder at the repo root is the default target of a bare `stark.run()`;
`examples/agents/` is a richer set covering MCP servers, scripts, and every discovery rule.

## `stark.run()`

```python
def run(
    agents: str = "./agents",
    listener: str = "cli",              # "cli" | "slack"
    exclude_agents: list[str] | None = None,
    instructions: str = "You're an helpful assistant. Use any relevant tool at your disposal to answer the user query."
) -> None
```

| Argument | Meaning |
| --- | --- |
| `agents` | Root folder holding one subdirectory per agent. |
| `listener` | `"cli"` for an interactive prompt, `"slack"` for mentions and DMs over Socket Mode. |
| `exclude_agents` | Directory names inside `agents` to skip during discovery. |
| `instructions` | The master system prompt for the orchestration loop. |

`run()` blocks until interrupted. To embed it in an existing event loop, use
`await stark.run_async(...)` — same arguments.

## The AGENT.md schema

### Mandatory

| Key | Example |
| --- | --- |
| `name` | `research-agent` |
| `description` | What the agent does — the orchestrator routes on this, so make it specific. |
| `provider` | `anthropic`, `openai`, `gemini`, … (any LiteLLM provider) |
| `model` | `claude-opus-5` |

If any of these is missing, Stark logs a warning and skips that agent. The rest keep loading.

### Optional

| Key | Default | Meaning |
| --- | --- | --- |
| `effort` | `medium` | Reasoning effort: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`. |
| `max_iterations` | `100` | Tool-calling turns before the agent stops. |
| `max_output_tokens` | `4096` | Output cap per turn. |
| `base_url` | `""` | Override the provider endpoint (e.g. a LiteLLM proxy). |
| `api_key` | `""` | Override the provider key for this agent. |
| `mcp` | *(none)* | A **list** of MCP servers — see below. |

String values support `${VAR}` and `${VAR:-fallback}` environment expansion, so secrets
stay out of the file:

```yaml
api_key: ${RESEARCH_AGENT_KEY}
base_url: ${LLM_PROXY_URL:-https://api.anthropic.com}
```

### Discovery rules

1. Every agent directory must have an `AGENT.md` **at its root** — nested ones are not found.
2. A directory without `AGENT.md` is skipped silently.
3. A directory named in `exclude_agents` is skipped.
4. Missing mandatory metadata → warning, skip that agent.
5. Duplicate `name` values → the first one wins, the rest are skipped with a warning.

Only a missing `agents` directory is fatal.

## MCP servers

`mcp:` is a list of servers. Stark walks it and starts each entry whose `enable` is true.

```yaml
---
name: jira-agent
description: Creates and transitions Jira tickets, and posts to Slack.
provider: anthropic
model: claude-opus-5
mcp:
  - name: slack
    enable: true
    command: uvx
    args: ["mcp-slack"]
    exclude: ["send_message"]              # deny-list

  - name: atlassian
    enable: true
    command: uvx
    args: ["mcp-atlassian"]
    env:
      JIRA_URL: ${JIRA_URL}
      JIRA_USERNAME: ${JIRA_EMAIL}
      JIRA_API_TOKEN: ${JIRA_TOKEN}

  - name: remote
    enable: false                          # parked: defined, never started
    transport: streamable_http
    url: https://mcp.example.com/mcp
    headers:
      Authorization: Bearer ${MCP_TOKEN}
    include: ["search", "fetch"]           # allow-list, wins over exclude
---
```

| Field | Applies to | Notes |
| --- | --- | --- |
| `name` | both | **Required.** Identifies the server in logs and errors. Must be unique per agent. |
| `enable` | both | Defaults to `true` — listing a server is intent to use it. Set `false` to keep an entry without starting it. |
| `transport` | both | `stdio` (default) or `streamable_http`. |
| `command`, `args`, `env` | stdio | **`command` required.** `env` merges over a minimal safe environment; cwd is the agent's directory. |
| `url`, `headers` | streamable_http | **`url` required.** |
| `include` / `exclude` | both | Filter which tools reach the model. `include` takes precedence. |

Omitting `mcp:` entirely means the agent has no MCP servers — nothing is spawned
speculatively. Enabled servers start once during boot and are reused for every query.

Malformed entries never break discovery: an entry missing `name`, a stdio entry missing
`command`, an unknown `transport`, or a duplicate name is logged and skipped, and the agent
loads with whatever remains. A server that fails to *start* is likewise logged and dropped.

> The MCP server runs as a subprocess, so `command` must be an interpreter or binary that
> has the server's own dependencies installed. Its working directory is the agent's folder,
> which is why `args: ["server.py"]` resolves.

## Built-in workspace tools

Every agent gets three tools scoped to its own directory. Paths are resolved and checked,
so an agent cannot reach outside its folder.

| Tool | Purpose |
| --- | --- |
| `workspace_list` | List the agent's files, optionally by glob. |
| `workspace_read` | Read one of its text files. |
| `workspace_run` | Run one of its scripts and return exit code, stdout, and stderr. |

`workspace_run` executes `.py` files on the current interpreter and any other executable
directly, with a 120s default timeout (900s max). This is what makes
"run `find_research.py` when asked to research something" work with no glue code.

## How a query flows

```
user query
   │
   ├─ orchestrator (master instructions + agent roster)
   │     │
   │     ├─ agent__research-agent ─┐   spawned in parallel, each with its own
   │     └─ agent__example-agent ──┤   context, model, tools and budgets
   │                               │
   │     ┌─────────────────────────┘
   │     │  agent results returned as tool output
   │     ▼
   └─ final answer streamed to the listener
```

Each agent runs its own tool-calling loop and sees only the task it was handed — never the
orchestrator's conversation. That keeps contexts small and makes the roster composable. The
orchestrator can chain agents by passing one's findings into another's `context` field.

## Configuring the orchestrator

`run()` has a fixed signature, so the orchestration loop's own model comes from the
environment. Anthropic is the default.

| Variable | Default |
| --- | --- |
| `STARK_PROVIDER` | `anthropic` |
| `STARK_MODEL` | `claude-opus-5` |
| `STARK_EFFORT` | `medium` |
| `STARK_MAX_ITERATIONS` | `100` |
| `STARK_MAX_OUTPUT_TOKENS` | `4096` |
| `STARK_BASE_URL` | *(provider default)* |
| `STARK_API_KEY` | *(provider default)* |

Provider credentials themselves follow LiteLLM's conventions — `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, and so on.

## Listeners

### CLI

```bash
stark --agents ./agents
```

Streams the answer as it generates, and prints a dim progress line per delegation and tool
call. `/agents` lists the roster, `/exit` quits.

Each query closes with a summary line — wall-clock time first, then whatever else the run
produced:

```
stark › EMEA Q2 sales were $4,480,000; top product Atlas Pro.
  · 4.12s · 2 iteration(s) · 1 agent call(s) · $0.0122
```

The elapsed time covers the whole query: every model turn, delegation and tool call. It is
reported even when the query fails, so a slow failure is still visible. Cost and agent
counts are omitted when they are zero. This footer is CLI-only — Slack replies are
unchanged.

### Slack

```bash
export SLACK_BOT_TOKEN=xoxb-...   # bot token
export SLACK_APP_TOKEN=xapp-...   # app-level token, Socket Mode enabled
stark --agents ./agents --listener slack
```

Responds to `@mentions` in channels and to direct messages, replying in-thread. It posts a
placeholder immediately and edits it as the answer streams in. Subscribe your app to the
`app_mention` and `message.im` events.

Both tokens must be present or startup fails with a clear error before any MCP server boots.

## CLI reference

```
stark [--agents PATH] [--listener {cli,slack}] [--exclude NAME]... [--instructions TEXT] [--verbose]
```

`python -m stark` works identically.

## Python API

Beyond `run()`, the pieces are importable if you want to embed them:

```python
from stark import Orchestrator, Registry, discover_agents, parse_agent_file

agents = discover_agents("./agents", exclude_agents=["wip-agent"])

registry = await Registry.create("./agents")
try:
    orchestrator = Orchestrator(registry, "Master instructions.", stark.orchestrator_model())
    result = await orchestrator.handle(stark.Message(text="..."), my_sink)
    print(result.output, result.cost, result.agent_results)
finally:
    await registry.aclose()   # must run in the task that created the registry
```

`RunResult` carries `output`, `iterations`, `cost`, `error`, `max_iterations_reached`, and
an `agent_results` list of per-agent `AgentResult` records.

To send output somewhere Stark does not support yet, implement `ResponseSink` (`chunk`,
`final`, `error`, and optionally `event` and `status`) and pass it to `Orchestrator.handle`.

## Layout

```
src/stark/
├── runtime.py          # run() / run_async(): wires the three startup steps together
├── types.py            # AgentConfig, ModelConfig, Completion, RunResult, …
├── parsers/            # frontmatter, AGENT.md validation, directory discovery
├── mcp/                # stdio + streamable-HTTP clients, per-agent manager
├── llm/                # LiteLLM wrapper: request building, streaming, cost
├── orchestration/      # registry, per-agent runner, master loop
├── listeners/          # base contracts, cli, slack
└── tools/              # the built-in workspace toolset
```

## Development

```bash
uv sync --extra dev --extra slack
.venv/bin/python -m pytest
```

The suite covers discovery and validation, MCP config parsing, live MCP integration
against a real stdio server, the workspace sandbox, Slack dispatch, and the full
orchestration loop against a stubbed model — no network calls.

## License

Apache-2.0. See [LICENSE](LICENSE).
