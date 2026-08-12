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
- ⚙️ **Deterministic script agents** — trigger a plain Python `run()` on a rule match, with no model in the path at all.
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
    instructions: str = "You're an helpful assistant. Use any relevant tool at your disposal to answer the user query.",
    config: Config | dict | None = None,
) -> None
```

| Argument | Meaning |
| --- | --- |
| `agents` | Root folder holding one subdirectory per agent. |
| `listener` | `"cli"` for an interactive prompt, `"slack"` for mentions and DMs over Socket Mode. |
| `exclude_agents` | Directory names inside `agents` to skip during discovery. |
| `instructions` | The master system prompt for the orchestration loop. |
| `config` | Presentation settings — currently the Slack progress icons. See [Customising the icons](#customising-the-icons). |

`run()` blocks until interrupted. To embed it in an existing event loop, use
`await stark.run_async(...)` — same arguments.

## Two agent types

`type` is optional and defaults to `llm`, so an AGENT.md without it behaves exactly as before.

| | `type: llm` | `type: script` |
| --- | --- | --- |
| Runs | a model, with tools | a Python `run()` function — **no model** |
| Offered to the orchestrator | yes | **never** |
| Reached by | the orchestrator's routing decision | `triggerRule`, or unconditionally |
| Needs a provider/model | yes | no |

A script agent is for work that must happen the same way every time — open a ticket, tag a
request, call an internal API. Nothing is left to a model's judgement, and no tokens are
spent deciding whether to run it.

## The AGENT.md schema

### Mandatory

| Key | `llm` | `script` |
| --- | --- | --- |
| `name` | ✓ | ✓ |
| `description` | ✓ (the orchestrator routes on this) | ✓ (documentation only) |
| `provider` | ✓ | — |
| `model` | ✓ | — |
| `type` | — (defaults to `llm`) | ✓ (`script`) |
| `script` | — | ✓ (a file in the agent's folder) |

If a mandatory key is missing, Stark logs a warning and skips that agent. The rest keep loading.

### Optional — `llm` agents

| Key | Default | Meaning |
| --- | --- | --- |
| `effort` | `medium` | Reasoning effort: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`. |
| `max_iterations` | `100` | Tool-calling turns before the agent stops. |
| `max_output_tokens` | `4096` | Output cap per turn. |
| `base_url` | `""` | Override the provider endpoint (e.g. a LiteLLM proxy). |
| `api_key` | `""` | Override the provider key for this agent. |
| `mcp` | *(none)* | A **list** of MCP servers — see below. |

### Optional — `script` agents

| Key | Default | Meaning |
| --- | --- | --- |
| `triggerRule` | *(none — runs on every message)* | An expression deciding whether this agent runs. |
| `priority` | `100` | Higher runs earlier. Agents sharing a priority run in parallel. |
| `send_output` | `false` | Whether the output is also posted to the user. |
| `timeout` | `120` | Seconds before `run()` is abandoned. |

Metadata that belongs to the other type is ignored with a warning, so a `triggerRule` on an
`llm` agent tells you it does nothing rather than silently never firing.

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

## Script agents

A script agent declares a file exposing `run()`. That's the whole contract:

```yaml
---
name: ticket-opener
description: Opens a tracking ticket for escalations.
type: script
script: open_ticket.py
priority: 200
send_output: true
triggerRule: 'text.contains("=====") and channel.notContains("PODUEMCJE")'
---
```

```python
def run(message: dict) -> str:            # `async def run(...)` also works
    return f"Opened SUPPORT-123 for {message['user']}"
```

`run()` receives a plain dict — deliberately not a Stark object, so the file imports
nothing from `stark` and can be unit-tested on its own:

| Key | Contents |
| --- | --- |
| `text` | the message, mention stripped |
| `user`, `channel`, `thread` | listener identifiers (see the table below) |
| `meta` | the raw listener payload |
| `agent`, `workspace` | this agent's name and directory |
| `prior_outputs` | `[{agent, output, error}]` from higher-priority bands |

Return a string, or anything JSON-serialisable. Raising is safe — the phase is fail-open, so
the error is shown as a failed step and passed to the orchestrator as context.

A synchronous `run()` is executed in a worker thread so blocking I/O can't stall the event
loop, MCP sessions, or other in-flight queries. The `timeout` abandons a wedged call but
cannot kill the thread, so an infinite loop leaks one thread for the life of the process.

### `triggerRule`

One expression, in the form `field.operator("literal")` joined by `and` / `or` / `not` and
parentheses:

```yaml
triggerRule: '(text.contains("ABC") and text.contains("XYZ")) or not channel.contains("PODUEMCJE")'
```

- **Operators:** `contains`, `notContains`. Matching is case-insensitive.
- **Fields:** `text`, `user`, `channel`, `thread`.
- Parsed at **startup**, so a malformed rule names its own position and disables only that
  agent — the rest keep loading.
- Never `eval`'d. An AGENT.md is configuration; evaluating it would make any agent folder a
  code-execution vector.
- **Always quote the expression.** Unquoted YAML turns `@here` and `*urgent*` into parse
  errors, `no`/`off` into booleans, and `#tag` into a comment.

What each listener populates — the fields a rule can actually read:

| Field | CLI | Slack |
| --- | --- | --- |
| `text` | what you typed | message text, mention stripped |
| `user` | `"cli"` | user id (`U…`) |
| `channel` | **`None`** | channel **id** (`C…`), not a name |
| `thread` | **`None`** | thread timestamp |

Two consequences worth knowing. `channel` is an **id**, so matching a human channel name
never fires. And an absent field makes `contains` false but `notContains` **true**, so a
channel guard passes vacuously under the CLI — handy for testing the text half of a rule,
but it is not a guard off Slack.

## How a query flows

```
user query
   │
   ├─ script phase — deterministic, no model
   │     band 200 : A, B    matching agents run in parallel
   │     band 100 : C       then this band, seeing A+B's output
   │        │
   │        │  send_output: true  → posted to the user AND passed on
   │        │  send_output: false → passed on only
   │        ▼
   ├─ orchestrator (master instructions + llm roster) ── runs only if an llm agent exists
   │     │
   │     ├─ agent__research-agent ─┐   spawned in parallel, each with its own
   │     └─ agent__example-agent ──┤   context, model, tools and budgets
   │                               │
   │     ┌─────────────────────────┘
   │     │  agent results returned as tool output
   │     ▼
   └─ final answer streamed to the listener
```

Each `llm` agent runs its own tool-calling loop and sees only the task it was handed — never
the orchestrator's conversation. That keeps contexts small and makes the roster composable.

Script agents run first, in descending priority bands. Bands are sequential and each one
sees everything the earlier bands produced, so ordering can express a real dependency —
open the ticket, then notify about it. Agents sharing a priority run concurrently, on the
assumption they're independent.

The orchestrator always receives what the scripts produced, labelled with whether the user
has already seen it, so it builds on that output instead of repeating it.

**With no `llm` agents registered, no model is called at all.** Stark then runs as a purely
deterministic router — no API key, no token spend. Silence is a valid outcome in that mode:
the script steps are the user's confirmation that work happened.

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

Responds to `@mentions` in channels and to direct messages, replying in-thread. Subscribe
your app to the `app_mention` and `message.im` events.

**The answer is not streamed.** Slack gets two messages: a live progress message, and the
finished answer posted once. Each agent delegation and tool call appears as a
`:hourglass:` line while it runs, then is struck through with `:white_check_mark:` when it
completes — tool calls nested under the agent that ran them:

```
:white_check_mark: ~sales-agent: What were EMEA sales in Q2?~
        ↳ :white_check_mark: ~sales-agent → workspace_run~
:hourglass: inventory-agent: Is ATL-LITE-002 in stock?
        ↳ :hourglass: inventory-agent → check_stock
```

A step that fails is struck with `:x:` instead, and nothing is ever left spinning —
anything still running when the query ends is settled.

Edits are coalesced rather than sent per event: the first change goes out immediately and
changes during the cooldown collapse into one edit, which keeps a wide parallel run inside
Slack's ~1 edit/second limit. On a fast query several steps may therefore go from unseen to
struck in a single edit.

### Customising the icons

Pass `config` to `stark.run()`. The three defaults above are all built-in Slack emoji, so
they work in any workspace — a custom shortcode has to be added to your workspace first, or
Slack renders the literal `:name:` text.

```python
stark.run(
    listener="slack",
    config={
        "slack": {
            "running_emoji": ":cyclone:",
            "done_emoji": ":heavy_check_mark:",
            "failed_emoji": ":no_entry_sign:",
        }
    },
)
```

A typed form is available if you prefer discoverability:

```python
from stark import Config, SlackConfig

stark.run(listener="slack", config=Config(slack=SlackConfig(running_emoji="⏳")))
```

| `config["slack"]` key | Default | Meaning |
| --- | --- | --- |
| `running_emoji` | `:hourglass:` | Shown while a step is in progress |
| `done_emoji` | `:white_check_mark:` | Shown when a step completes, alongside strikethrough |
| `failed_emoji` | `:x:` | Shown when a step fails, and prefixes an error reply |
| `starting_label` | `Working on it` | The placeholder line before any step exists |
| `update_interval` | `1.2` | Seconds between `chat.update` calls |

Emoji may be a shortcode (`:hourglass:`) or a literal character (`⏳`). Unlike AGENT.md
metadata — which is parsed forgivingly, because one bad file shouldn't stop the process —
an unknown `config` key raises immediately. A silently ignored typo here would mean a
customisation that never takes effect.

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
