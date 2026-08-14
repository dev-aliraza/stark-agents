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
- ⚙️ **Deterministic script agents** — a plain Python `run()` with no model in the path: delegated to like any other agent, or fired by a rule before or after the orchestrator. One can halt the whole query.
- 🔌 **Native MCP** — declare stdio or streamable-HTTP servers in frontmatter; they start once at boot and stay warm.
- 🧠 **Model agnostic** — LiteLLM underneath, so any of its 100+ providers works. Anthropic is the first-class default.
- 🎧 **Pluggable listeners** — an interactive CLI or a Slack Socket Mode bot, same orchestration behind both.
- ⚡ **Parallel delegation** — independent sub-tasks fan out concurrently; so do the tool calls inside each agent.
- 🛠️ **Built-in file tools** — every agent can list, read, write, delete, and run files in its own directory, sandboxed to it.
- 🌐 **Native tools** — a `tools:` block gives an agent a shell or web search; `file` is global. In-process, so settings are settings, not environment smuggled through a subprocess.
- 🧱 **Per-agent budgets** — each agent carries its own model, effort, iteration cap, and token cap.

## Installation

```bash
pip install stark-agents
```

For the Slack listener, web search, or a proper CLI prompt:

```bash
pip install 'stark-agents[slack]'
pip install 'stark-agents[websearch]'
pip install 'stark-agents[cli]'
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
| `listener` | `"cli"` for an interactive prompt, `"slack"` for a Socket Mode bot. |
| `exclude_agents` | Directory names inside `agents` to skip during discovery. |
| `instructions` | The master system prompt for the orchestration loop. |
| `config` | Listener settings and the orchestrator's own tools. See [Choosing what to listen to](#choosing-what-to-listen-to) and [Tools for the orchestrator](#tools-for-the-orchestrator). |

`run()` blocks until interrupted. To embed it in an existing event loop, use
`await stark.run_async(...)` — same arguments.

## Two agent types

`type` is optional and defaults to `llm`, so an AGENT.md without it behaves exactly as before.

| | `type: llm` | `type: script` |
| --- | --- | --- |
| Runs | a model, with tools | a Python `run()` function — **no model** |
| Offered to the orchestrator | yes | yes, unless `avoid_orchestrator: true` |
| Also runs on its own | no | only if it sets a `triggerPoint` |
| Needs a provider/model | yes | no |

A script agent is for work that must happen the same way every time — open a ticket, tag a
request, call an internal API. Nothing is left to a model's judgement, and no tokens are
spent deciding whether to run it.

It has two independent ways in:

* **By delegation**, when the orchestrator decides the request calls for it. On by default;
  `avoid_orchestrator: true` turns it off.
* **Automatically**, whenever its `triggerRule` matches. Off by default; a `triggerPoint`
  turns it on and says whether it happens before or after the orchestrator.

So a script agent with no `triggerPoint` is just a deterministic tool: it sits in the
orchestrator's tool list and waits to be asked. Adding `triggerPoint` is what makes it fire
on its own as well.

Two combinations are worth avoiding, and Stark warns about both at startup:

* A `triggerRule` with **no** `triggerPoint` does nothing — there is no automatic run for it
  to gate, and delegation does not consult it.
* `avoid_orchestrator: true` with **no** `triggerPoint` leaves nothing able to run the agent
  at all.

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
| `tools` | *(`file` only)* | Native capabilities: `shell`, `websearch`, and settings for `file`. |

### Optional — `script` agents

| Key | Default | Meaning |
| --- | --- | --- |
| `triggerPoint` | *(none — the agent never runs on its own)* | `before_orchestrator` or `after_orchestrator`: gives the agent an automatic run, on that side of the orchestrator. |
| `triggerRule` | *(none — the automatic run fires on every message)* | An expression deciding which messages that automatic run fires for. Needs a `triggerPoint` to do anything. |
| `priority` | `100` | Higher runs earlier, within its own `triggerPoint`. Agents sharing a priority run in parallel. |
| `send_output` | `false` | Whether the output is also posted to the user. |
| `avoid_orchestrator` | `false` | `true` withholds the agent from the orchestrator's tool list. |
| `timeout` | `120` | Seconds before `run()` is abandoned. |

Omitting `triggerPoint` is safe by design — the agent only runs when asked. A *wrong* value
is rejected outright, since defaulting it would silently move the agent to the wrong side of
the model.

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

## Native tools

`tools:` declares the capabilities Stark ships, running **in-process**. `mcp:` is for
third-party servers; this is for Stark's own.

```yaml
tools:
  file:
    exclude: [file_delete]          # keep write, drop delete
  shell:
    allow: [git, ls, rg]            # only these programs
    cwd: ${REPO_PATH:-.}
    timeout: 60
  websearch:
    search_provider: brave
    search_key: ${BRAVE_SEARCH_API_KEY:-}
    search_key: ${BRAVE_SEARCH_API_KEY:-}
```

Three shapes are accepted, because `shell:` with nothing after it is easy to mistype:

```yaml
tools: [shell, websearch]          # nothing to configure
tools:
  shell:                         # defaults
  websearch: {}                  # the same, spelled out
```

| Key | Applies to | Meaning |
| --- | --- | --- |
| `enable` | any tool | `false` parks a configured tool without deleting the block. Defaults true. |
| `include` | any tool | An allowlist of individual tool names. |
| `exclude` | any tool | Individual tool names to withhold. |

Settings are relative to the tool — `search_key`, not `websearch_search_key` — and `${VAR}`
expansion works throughout. An unknown tool or setting is warned about and dropped: an
AGENT.md is authored config, and one bad key should cost you that key, not the agent.

Because these run in-process, their settings are just settings. There is no `env:` block to
forward a key through and no interpreter to point at, which are the two ways an MCP-hosted
tool silently ends up misconfigured.

### `file` is global

Every agent **and the orchestrator** gets `file` without asking, because it is confined to one
directory — the agent's own folder, and for the orchestrator the agents directory. That
confinement is what makes it safe to hand out. A `tools: file:` entry only ever configures or
removes it:

| Tool | Purpose |
| --- | --- |
| `file_list` | List the agent's files, optionally by glob. |
| `file_read` | Read one of its text files (truncated at 40,000 chars). |
| `file_write` | Create a text file, or replace one with `overwrite: true`. |
| `file_delete` | Delete a file, or an empty folder. |
| `file_run` | Run one of its scripts and return exit code, stdout, and stderr. |

```yaml
tools:
  file:
    exclude: [file_write, file_delete, file_run]   # read-only
  # or:
  file:
    enable: false                                  # no file access at all
```

`file_run` executes `.py` files on the current interpreter and any other executable directly,
with a 120s default timeout (900s max). That is what makes "run `find_research.py` when asked
to research something" work with no glue code — and together with `file_write`, it means an
agent can generate a script and then run it.

**Writing and deleting carry guards a read-only tool does not need:**

- **No accidental clobbering.** `file_write` refuses to replace an existing file unless
  `overwrite: true` is passed. Without that, an agent that thinks it is creating a fresh file
  silently destroys one.
- **Refused, not truncated.** Content over 100,000 characters is rejected. Half a file written
  and reported as success is worse than an error.
- **No recursive delete.** A folder with anything in it is refused.
- **`AGENT.md` is off limits.** An agent cannot rewrite or delete its own definition.

What the sandbox bounds is worth being precise about: the **paths** an agent may name, not the
process it starts. A script reached through `file_run` is ordinary local code with your user's
permissions.

### `shell`

Never handed out by default — declare it, per agent.

| Tool | Purpose |
| --- | --- |
| `shell_run` | Run a command; returns exit code, stdout, stderr and duration. |
| `shell_which` | Check whether a program is installed, and where. |
| `shell_policy` | Report what this tool will and will not run. |

| Setting | Meaning |
| --- | --- |
| `allow` | Programs that may run. Omit for no restriction. |
| `cwd` | Where commands run, relative to the agent's folder. |
| `timeout` | Default seconds before a command is killed. |

Commands go through the shell, so pipes, redirection and globs work. They run with **no stdin
and no terminal**, so anything that would prompt fails immediately instead of hanging — pass
every answer as a flag.

**Be clear about what this is.** It runs commands as the user the process runs as, with that
user's permissions. **It is not a sandbox.** What the guards do:

- **Bound the runtime.** 120s default, 900s maximum, and a timeout kills the whole process
  group — killing only the shell would orphan whatever it started.
- **Bound the output.** stdout and stderr are truncated at 20,000 characters each.
- **Catch a few catastrophic mistakes.** `rm -rf /`, `mkfs`, `dd of=/dev/disk2`, a fork bomb,
  `curl … | sh`, `shutdown`. These catch a model that has misread its instructions — not
  someone determined to get past them. The list is short on purpose, and `rm -rf ./build`
  still works.

Two things provide real containment, and both are yours to set. **`allow`** — only these
programs run, and in that mode shell metacharacters are refused too, because an allowlist that
checks the first word alone is defeated by `git status; rm -rf ~`. So an allowlist means one
plain command per call, no pipes. And **not declaring it** for an agent that has no business
running commands.

### `websearch`

```bash
pip install 'stark-agents[websearch]'
```

| Tool | Purpose |
| --- | --- |
| `websearch_search` | Search the web; returns title, URL and snippet as data. |
| `websearch_open` | Fetch a page and return its readable text. |

| Setting | Meaning |
| --- | --- |
| `search_provider` | `brave`, `serper` or `duckduckgo`. Defaults to whichever key is set. |
| `search_key` | The API key for that provider. |
| `allow_private` | `true` permits localhost and private-network URLs. Off by default. |

**There is no browser in this toolset.** Pages are fetched over HTTP and turned into text by
a stdlib extractor, so the only dependency is httpx — no browser binary, no driver, nothing
to install beyond the extra. A page that builds itself with JavaScript comes back with no
text, and the reply says so instead of pretending.

**Don't transcribe the human's UI steps.** "Open google.com, type in the box, click the first
result" is how a person does it. `websearch_search` returns URLs, so following a result is
`websearch_open` — no page layout, no typing, no search box that may have moved. A research
task is two tool calls:

```
websearch_search("top 10 destinations in UAE")   → [{title, url, snippet}, ...]
websearch_open(<the most trustworthy url>)       → readable text
```

Then the model summarises from text it already has. **Summarising is not a tool** — tools
fetch, the model reasons. [`examples/agents/web-agent`](examples/agents/web-agent) is exactly
this, and `examples/06_web_research.py` runs it.

#### Search providers

| Env var | Provider |
| --- | --- |
| `BRAVE_SEARCH_API_KEY` | Brave Search API (preferred) |
| `SERPER_API_KEY` | Serper (Google results) |
| *(none)* | DuckDuckGo HTML — best-effort, no key |
| `STARK_SEARCH_PROVIDER` | Force `brave`, `serper` or `duckduckgo` |

`search_provider` and `search_key` override the environment per agent, so two agents can
search through different providers.

The keyless DuckDuckGo fallback keeps the shipped example runnable with no signup. It parses
an HTML page and will break when that page changes — it says so rather than reporting nothing
found. Driving google.com is the least reliable option of all: consent dialogs, bot detection,
weekly layout changes, and it is against their terms.

#### What it refuses

Non-public addresses — `localhost`, private ranges and `169.254.169.254`, **including after a
redirect**, since otherwise "fetch this URL" reaches your cloud metadata endpoint. Non-http
schemes. Oversized and binary responses.

One thing it cannot refuse for you: **page content is untrusted input.** A page can contain
text aimed at the model. This toolset only reads, which bounds the damage to a bad summary —
an agent that could also act on a page is a different proposition.

### Tools for the orchestrator

The orchestrator is not an agent and has no `AGENT.md`, so it declares tools through `config`:

```python
stark.run(config={"orchestrator": {"tools": {"shell": {"allow": ["git"]}}}})
```

`file` is on for it as for everyone, rooted at the agents directory — the one directory it can
be said to own. `config={"orchestrator": {"root": "/some/path"}}` moves it.

Weigh this before adding anything verbose. A tool result here lands in the conversation and is
**re-sent on every later turn**, where an agent's own work is discarded once it reports back.
Reading one short file directly is cheaper than a delegation; reading six is not. The
orchestrator's prompt says as much, and tells it to prefer an agent for anything substantial.

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
triggerPoint: before_orchestrator
triggerRule: 'text.contains("=====") and channel.notContains("PODUEMCJE")'
---
```

Drop the last two lines and you still have a working agent — one the orchestrator can
delegate to, which never fires on its own.

```python
def run(message: dict) -> str:            # `async def run(...)` also works
    return f"Opened SUPPORT-123 for {message['user']}"
```

`run()` receives a plain dict — deliberately not a Stark object, so the file imports
nothing from `stark` and can be unit-tested on its own:

| Key | Contents |
| --- | --- |
| `text` | the message, mention stripped — always the user's own words |
| `user`, `channel`, `thread` | listener identifiers (see the table below) |
| `meta` | the raw listener payload |
| `agent`, `agent_dir` | this agent's name and directory |
| `prior_outputs` | `[{agent, output, error}]` from everything that already ran this query |
| `invocation` | `"trigger"` or `"delegation"` — how this run was reached |
| `task`, `context` | what the orchestrator asked for; empty strings on a triggered run |
| `orchestrator_output` | the answer the orchestrator gave; empty unless `after_orchestrator` |

The keys are the same however the agent was reached, so one script can serve both a trigger
and a delegation. Return a string, or anything JSON-serialisable. Raising is safe — the phase
is fail-open, so the error is shown as a failed step and passed to the orchestrator as
context.

A synchronous `run()` is executed in a worker thread so blocking I/O can't stall the event
loop, MCP sessions, or other in-flight queries. The `timeout` abandons a wedged call but
cannot kill the thread, so an infinite loop leaks one thread for the life of the process.

### Stopping the run

A script can end the query outright by returning a mapping with `stop_execution: true`:

```python
def run(message: dict) -> dict:
    if already_handled(message["thread"]):
        return {"stop_execution": True, "output": "Ignored: already handled this thread."}
    return "Nothing to do."
```

Nothing downstream runs: later bands in the same phase, the orchestrator, and the after
phase are all skipped. What already happened stands — output a script posted stays posted,
and every step that ran is closed out normally, so nothing is left showing as in-progress.

- **`stop_execution` is a control signal, not output.** It is stripped before rendering. Give
  the user a message with an `output` key alongside it; any other keys are rendered as JSON.
- **Peers in the same band still complete.** They started concurrently, so by the time the
  flag is read they have already run. Give a gate its own higher `priority` if it must
  precede the work it guards.
- **A crash is not a stop.** A script that raises is still fail-open, and the run continues.
- **From a delegated call it stops the orchestrator too.** The current turn's tool results
  are discarded rather than sent back, because the model would otherwise answer around the
  halt. That leaves the reply as whatever the script posted, so a script that stops the run
  from a delegated call should set `send_output: true` and say something.

`RunResult.stopped_by` names the agent that halted the run, and `RunResult.stopped` is the
boolean.

### Delegation vs. triggering

Delegation is on by default; the triggered run is what `triggerPoint` adds. Set both and the
agent has both. They differ in four ways:

| | Triggered run | Delegated call |
| --- | --- | --- |
| Exists when | `triggerPoint` is set | `avoid_orchestrator` is not `true` |
| Decided by | `triggerRule` | the orchestrator |
| `triggerRule` consulted | yes | **no** — naming the agent is the decision |
| `task` / `context` | empty | what the model asked for |
| Output reaches the model | as context for the next turn | as the tool result |

`send_output: true` posts the raw output to the user either way. On a delegated call the tool
result is tagged as already posted, so the model builds on it instead of repeating it.

Pick by who should decide:

| Want | Set |
| --- | --- |
| The model decides when to act | nothing — that's the default |
| A rule decides, and nothing else | `triggerPoint` **and** `avoid_orchestrator: true` |
| Either can act | `triggerPoint` alone |
| It always runs, on every message | `triggerPoint`, no `triggerRule`, and `avoid_orchestrator: true` so the model cannot run it twice |

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
   ├─ script phase: triggerPoint: before_orchestrator — deterministic, no model
   │     band 200 : A, B    matching agents run in parallel
   │     band 100 : C       then this band, seeing A+B's output
   │        │
   │        │  send_output: true  → posted to the user AND passed on
   │        │  send_output: false → passed on only
   │        ▼
   ├─ orchestrator (master instructions + roster) ── runs only if an llm agent exists
   │     │
   │     ├─ agent__research-agent ─┐   llm agents: spawned in parallel, each with its
   │     ├─ agent__example-agent ──┤   own context, model, tools and budgets
   │     └─ agent__ticket-opener ──┤   script agents: run(), unless avoid_orchestrator
   │                               │
   │     ┌─────────────────────────┘
   │     │  results returned as tool output
   │     ▼
   ├─ final answer delivered to the listener
   │
   └─ script phase: triggerPoint: after_orchestrator
         band 100 : D       sees every earlier result, plus the answer itself
```

Any script agent along that path can cut it short by returning `stop_execution: true`:
everything below it in the diagram is skipped, and the reply is whatever had already been
posted.

Each `llm` agent runs its own tool-calling loop and sees only the task it was handed — never
the orchestrator's conversation. That keeps contexts small and makes the roster composable.

A script agent takes part in a phase only if it sets a `triggerPoint`; without one it appears
solely in the orchestrator's tool list, in the middle of the diagram. Phases run in
descending priority bands. Bands are sequential and each one sees everything the earlier
bands produced, so ordering can express a real dependency — open the ticket, then notify
about it. Agents sharing a priority run concurrently, on the assumption they're independent.
Priorities are ranked within each side of the orchestrator, not across both.

The orchestrator always receives what the before-phase scripts produced, labelled with
whether the user has already seen it, so it builds on that output instead of repeating it.

An `after_orchestrator` agent runs once the answer is out, and gets it as
`orchestrator_output`. That makes it the place for anything that acts *on* the reply —
archive it, file it, notify on it. Its own output cannot reach the model: that turn is over.
It still runs when no orchestrator did, with `orchestrator_output` empty.

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

#### Pasting a multi-line prompt

With `pip install 'stark-agents[cli]'` a pasted prompt **lands in the buffer**, so you can
read it, edit it, and press Enter to send. `Alt+Enter` adds a line without sending.

Without that extra it still arrives as one query rather than one query per line, but it sends
the moment the paste ends — there is no chance to review it. The reason is worth knowing:
`input()` returns a single line, and a pasted newline is indistinguishable from a typed Enter
(the terminal even maps a bare carriage return to one). GNU readline 8.1+ solves this with
bracketed paste, but macOS links Python's `readline` against **libedit**, which has no such
support. `prompt_toolkit` implements bracketed paste itself, which is why the extra exists.

A pipe is unaffected either way: `echo "..." | stark` keeps one query per line, because a line
editor cannot edit a pipe.

### Slack

```bash
export SLACK_BOT_TOKEN=xoxb-...   # bot token
export SLACK_APP_TOKEN=xapp-...   # app-level token, Socket Mode enabled
stark --agents ./agents --listener slack
```

By default it answers `@mentions` only, replying in-thread. Anything wider is opted into —
see [Choosing what to listen to](#choosing-what-to-listen-to).

**The answer is not streamed.** Slack gets two messages: a live progress message, and the
finished answer posted once. Each agent delegation and tool call appears as a
`:hourglass:` line while it runs, then is struck through with `:white_check_mark:` when it
completes — tool calls nested under the agent that ran them:

```
:white_check_mark: ~sales-agent: What were EMEA sales in Q2?~
        ↳ :white_check_mark: ~sales-agent → file_run~
:hourglass: inventory-agent: Is ATL-LITE-002 in stock?
        ↳ :hourglass: inventory-agent → check_stock
```

A step that fails is struck with `:x:` instead, and nothing is ever left spinning —
anything still running when the query ends is settled.

Edits are coalesced rather than sent per event: the first change goes out immediately and
changes during the cooldown collapse into one edit, which keeps a wide parallel run inside
Slack's ~1 edit/second limit. On a fast query several steps may therefore go from unseen to
struck in a single edit.

### Choosing what to listen to

`config["slack"]["events"]` decides what the bot sees. Omit it and only `app_mention` is
handled: the bot answers when named and is otherwise silent.

```python
stark.run(
    listener="slack",
    config={
        "slack": {
            "events": {
                "app_mention": True,                            # always
                "message.im": True,                             # direct messages
                "message.channels": 'text.contains("=====")',    # only these
            }
        }
    },
)
```

`True` listens to every one of those events, a string listens when that expression matches,
and `False` parks the line without deleting it. A plain list works when nothing needs
filtering: `"events": ["app_mention", "message.im"]`.

| Event | What it is | Scope it needs |
| --- | --- | --- |
| `app_mention` | the bot named in a channel | `app_mentions:read` |
| `message.im` | a direct message | `im:history` |
| `message.channels` | any message in a public channel it is in | `channels:history` |
| `message.groups` | any message in a private channel | `groups:history` |
| `message.mpim` | any message in a group DM | `mpim:history` |

These are Slack's own event names, because you have to subscribe to the same strings under
**Event Subscriptions** in your app config — one list, not two vocabularies. Scopes are
derived from what you enabled, so startup names the scope missing *for an event you asked
for* instead of a fixed list. `chat:write` is always required.

**The filter is the same expression language as an agent `triggerRule`** — `contains` and
`notContains` over `text`, `user`, `channel`, `thread`, joined with `and`/`or`/`not`. It runs
against the text the handler would see, with the mention stripped, and is parsed at startup
so a malformed expression fails before the socket opens.

The two layers are worth keeping distinct:

| | Listener filter | Agent `triggerRule` |
| --- | --- | --- |
| Decides | whether to respond at all | which script agent runs |
| On no match | total silence, nothing posted | the progress message still settles |

Two things to know once you go past mentions:

- **`channel` is an id** (`C0A1B2C3`), never a name, so `channel.contains("#support")` never
  fires. This is the same caveat as [`triggerRule`](#triggerrule).
- **Enabling `app_mention` and `message.channels` together is safe.** Slack delivers a
  channel mention as *both*, and the listener drops the duplicate copy.

### Bot-authored messages

Messages from other bots are ignored by default, which matters if your trigger source is an
alerting integration rather than a person:

```python
config={"slack": {"events": {"message.channels": 'text.contains("=====")'},
                  "allow_bots": True}}
```

Our own posts are always ignored regardless, since answering them is an unbounded loop.
`allow_bots` admits only the `bot_message` subtype — edits, deletions and channel joins stay
ignored either way, because none of them is a new question.

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
| `events` | `{"app_mention": True}` | Which events to handle, and an optional filter per event |
| `allow_bots` | `false` | Whether messages written by other bots are handled |

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
from stark import (
    TRIGGER_POINT_AFTER,
    TRIGGER_POINT_BEFORE,
    Orchestrator,
    Registry,
    ScriptPhase,
    discover_agents,
    parse_agent_file,
    stop_requested,
)

agents = discover_agents("./agents", exclude_agents=["wip-agent"])

registry = await Registry.create("./agents")
try:
    before = ScriptPhase(
        registry.script_agents_before, registry.script_runners(), TRIGGER_POINT_BEFORE
    )
    after = ScriptPhase(
        registry.script_agents_after, registry.script_runners(), TRIGGER_POINT_AFTER
    )
    orchestrator = Orchestrator(registry, "Master instructions.", stark.orchestrator_model())

    message = stark.Message(text="...")
    script_results = await before.run(message, my_sink)

    if stop_requested(script_results):        # a script halted the run
        await my_sink.final("")
    else:
        result = await orchestrator.handle(message, my_sink, script_results)
        if not result.stopped:
            result.script_results.extend(
                await after.run(message, my_sink, result.script_results, result.output)
            )
        print(result.output, result.cost, result.agent_results)
finally:
    await registry.aclose()   # must run in the task that created the registry
```

`run_async()` does exactly this, plus skipping the orchestrator when no `llm` agent is
registered. Calling `Orchestrator.handle` on its own is fine — the script phases are
optional, and it needs no script results.

`RunResult` carries `output`, `iterations`, `cost`, `error`, `max_iterations_reached`,
`stopped_by`, an `agent_results` list of per-agent `AgentResult` records, and a
`script_results` list of `ScriptResult` records.

Embedding the phases yourself means honouring `stop_execution` yourself: pass each phase's
results to `stop_requested()` and skip what follows if it returns a result.

To send output somewhere Stark does not support yet, implement `ResponseSink` (`chunk`,
`final`, `error`, and optionally `event`, `status`, `message` and `settle`) and pass it to
`Orchestrator.handle`.

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
└── tools/              # native toolsets: file, shell, websearch, and the catalog
```

## Development

```bash
uv sync --extra dev --extra slack
.venv/bin/python -m pytest
```

The suite covers discovery and validation, MCP config parsing, live MCP integration
against a real stdio server, the file sandbox, Slack dispatch, and the full
orchestration loop against a stubbed model — no network calls.

## License

Apache-2.0. See [LICENSE](LICENSE).
