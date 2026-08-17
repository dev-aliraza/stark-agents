"""Guard the shipped examples so they cannot rot.

These assert the claims `examples/README.md` makes about the example agent folder: what
loads, what is skipped, which tools each agent ends up with, and that the sales script
runs. Skipped automatically if the examples folder is not present (e.g. an installed
wheel).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from stark.orchestration import Registry
from stark.parsers import discover_agents

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
AGENTS = EXAMPLES / "agents"

# asyncio_mode = "auto" handles the async tests, so only the skip guard belongs here.
pytestmark = pytest.mark.skipif(
    not AGENTS.is_dir(), reason="examples/agents is not present"
)


@pytest.fixture(autouse=True)
def pinned_interpreter(monkeypatch):
    """inventory-agent starts its MCP server with ${PYTHON:-python3}.

    Point it at the interpreter running the tests, which is the one that has `mcp`
    installed — exactly what the example scripts do.
    """
    monkeypatch.setenv("PYTHON", sys.executable)


async def test_discovery_matches_the_documented_folder():
    names = {agent.name for agent in discover_agents(AGENTS)}

    # scratch/ has no AGENT.md, so it is skipped silently.
    assert names == {
        "sales-agent",
        "inventory-agent",
        "writer-agent",
        "draft-agent",
        "ticket-opener",
        "answer-archiver",
        "web-agent",
        "browser-agent",
        "vision-agent",
        "ops-agent",
    }


async def test_the_example_script_agent_is_wired_as_documented():
    agents = {agent.name: agent for agent in discover_agents(AGENTS)}
    ticket = agents["ticket-opener"]

    from stark.types import TRIGGER_POINT_BEFORE

    assert ticket.is_script
    assert ticket.script == "open_ticket.py"
    assert (ticket.priority, ticket.send_output) == (200, True)
    assert ticket.trigger_rule is not None
    # The trigger point is what gives the rule something to gate.
    assert ticket.trigger_point == TRIGGER_POINT_BEFORE
    assert ticket.runs_before_orchestrator is True
    # Hidden from the orchestrator, so the marker is the only way a ticket gets opened.
    assert ticket.avoid_orchestrator is True
    assert ticket.delegatable is False
    assert ticket.reachable is True
    # A script agent needs no model.
    assert (ticket.provider, ticket.model) == ("", "")

    assert ticket.triggered_by({"text": "===== outage =====", "channel": "C0SUP"}) is True
    assert ticket.triggered_by({"text": "ordinary question", "channel": "C0SUP"}) is False
    # The rule reads only `text`, so it fires in any channel and under the CLI too.
    assert ticket.trigger_rule.fields() == {"text"}
    assert ticket.triggered_by({"text": "===== outage =====", "channel": None}) is True


async def test_the_example_script_agent_runs():
    from stark.orchestration import ScriptRunner, load_entry_point

    agents = {agent.name: agent for agent in discover_agents(AGENTS)}
    ticket = agents["ticket-opener"]
    runner = ScriptRunner(ticket, load_entry_point(ticket))

    result = await runner.run(
        {"text": "===== ArgoCD is down =====", "user": "U1", "channel": "C1",
         "thread": "1.0", "meta": {}, "agent": "ticket-opener", "prior_outputs": []}
    )

    assert result.succeeded
    assert "SUPPORT-" in result.output
    assert "ArgoCD is down" in result.output


async def test_the_example_script_agent_is_idempotent_per_thread():
    """Same thread must yield the same reference, so a Slack redelivery cannot duplicate."""
    from stark.orchestration import ScriptRunner, load_entry_point

    agents = {agent.name: agent for agent in discover_agents(AGENTS)}
    ticket = agents["ticket-opener"]
    runner = ScriptRunner(ticket, load_entry_point(ticket))

    payload = {"text": "===== x =====", "user": "U1", "channel": "C1", "thread": "1.0",
               "meta": {}, "agent": "ticket-opener", "prior_outputs": []}
    first = await runner.run(dict(payload))
    second = await runner.run(dict(payload))

    assert first.output == second.output


async def test_the_example_after_orchestrator_agent_is_wired_as_documented():
    from stark.types import TRIGGER_POINT_AFTER

    agents = {agent.name: agent for agent in discover_agents(AGENTS)}
    archiver = agents["answer-archiver"]

    assert archiver.is_script
    assert archiver.trigger_point == TRIGGER_POINT_AFTER
    assert archiver.runs_after_orchestrator is True
    # Bookkeeping, not something a model should decide to invoke.
    assert archiver.avoid_orchestrator is True
    assert archiver.delegatable is False
    assert archiver.send_output is True
    # No rule, so it runs for every message.
    assert archiver.trigger_rule is None
    assert archiver.triggered_by({"text": "anything"}) is True


async def test_the_example_after_orchestrator_agent_files_the_answer():
    from stark.orchestration import ScriptRunner, build_payload, load_entry_point
    from stark.listeners.base import Message
    from stark.types import ScriptResult

    agents = {agent.name: agent for agent in discover_agents(AGENTS)}
    archiver = agents["answer-archiver"]
    runner = ScriptRunner(archiver, load_entry_point(archiver))

    payload = build_payload(
        archiver,
        Message(text="how did EMEA do?", user="U1", thread="1.0"),
        prior=[ScriptResult(agent="ticket-opener", output="SUPPORT-1234")],
        orchestrator_output="EMEA Q2 sales were $4,480,000.",
    )
    result = await runner.run(payload)

    assert result.succeeded
    assert "AUDIT-" in result.output
    assert "after ticket-opener" in result.output

    # Same thread and same answer must archive under the same reference.
    assert (await runner.run(build_payload(
        archiver,
        Message(text="how did EMEA do?", user="U1", thread="1.0"),
        prior=[ScriptResult(agent="ticket-opener", output="SUPPORT-1234")],
        orchestrator_output="EMEA Q2 sales were $4,480,000.",
    ))).output == result.output


async def test_the_example_after_orchestrator_agent_stays_quiet_with_no_answer():
    """The no-llm-agents case: nothing was answered, so there is nothing to file."""
    from stark.orchestration import ScriptRunner, build_payload, load_entry_point
    from stark.listeners.base import Message

    agents = {agent.name: agent for agent in discover_agents(AGENTS)}
    archiver = agents["answer-archiver"]
    runner = ScriptRunner(archiver, load_entry_point(archiver))

    result = await runner.run(build_payload(archiver, Message(text="hi")))

    assert result.succeeded
    assert result.output == ""


async def test_the_example_web_agent_is_wired_as_documented():
    agents = {agent.name: agent for agent in discover_agents(AGENTS)}
    web = agents["web-agent"]

    assert web.is_llm
    # A native toolset now, not an MCP subprocess: no command, no interpreter to get wrong.
    assert web.mcp == []
    browser = next(tool for tool in web.tools if tool.name == "websearch")

    # Read-only: the agent can search and read, but cannot act on a page.
    assert browser.exclude == []
    assert set(browser.settings) <= {"search_provider", "search_key"}


async def test_the_example_web_agent_settings_are_plain_config(monkeypatch):
    """The env: block this needed as a subprocess is gone — settings are just settings."""
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")

    agents = {agent.name: agent for agent in discover_agents(AGENTS)}
    browser = next(tool for tool in agents["web-agent"].tools if tool.name == "websearch")

    assert browser.settings["search_key"] == "test-key"
    

async def test_an_unset_search_key_does_not_masquerade_as_configured(monkeypatch):
    """`${VAR:-}` yields "", which must read as absent rather than as a key."""
    for name in ("BRAVE_SEARCH_API_KEY", "SERPER_API_KEY", "STARK_SEARCH_PROVIDER"):
        monkeypatch.delenv(name, raising=False)

    agents = {agent.name: agent for agent in discover_agents(AGENTS)}
    browser = next(tool for tool in agents["web-agent"].tools if tool.name == "websearch")
    assert browser.settings["search_key"] == ""

    from stark.tools.websearch import WebSearchTools
    from stark.tools.websearch.providers import DUCKDUCKGO, choose_provider

    built = WebSearchTools(None, browser.settings)
    assert choose_provider(built.search_env()) == DUCKDUCKGO


async def test_the_example_browser_agent_is_wired_as_documented():
    agents = {agent.name: agent for agent in discover_agents(AGENTS)}
    agent = agents["browser-agent"]

    assert agent.is_llm
    assert agent.mcp == []
    browser = next(tool for tool in agent.tools if tool.name == "browser")
    assert set(browser.settings) <= {
        "host", "port", "token", "timeout", "connect_timeout", "vision",
    }
    assert browser.settings["vision"] is True


async def test_the_example_browser_agent_binds_no_port_until_a_tool_is_called():
    """Declaring the toolset must not open a socket — most runs never browse."""
    from stark.tools.browser.bridge import _BRIDGES

    registry = await Registry.create(AGENTS, exclude_agents=["draft-agent", "web-agent"])
    try:
        agent = registry.agent_for("agent__browser-agent")
        names = {
            schema["function"]["name"]
            for schema in registry.toolbox_for(agent).schemas()
        }
        assert "browser_open" in names and "browser_fill" in names
        assert _BRIDGES == {}
    finally:
        await registry.aclose()


async def test_the_example_web_agent_gets_only_the_read_tools():
    """The `exclude:` list is what makes this agent read-only, so prove it takes effect."""
    pytest.importorskip("httpx", reason="the websearch tool needs the [websearch] extra")

    registry = await Registry.create(
        AGENTS,
        exclude_agents=[
            "draft-agent", "sales-agent", "inventory-agent", "writer-agent", "ops-agent",
        ],
    )
    try:
        web = registry.agent_for("agent__web-agent")
        names = {
            schema["function"]["name"] for schema in registry.toolbox_for(web).schemas()
        }
        assert {"websearch_search", "websearch_open"} <= names
        # file is global, so it is there without being declared.
        assert {"file_read", "file_list"} <= names
    finally:
        await registry.aclose()


async def test_the_example_ops_agent_has_an_allowlisted_shell():
    agents = {agent.name: agent for agent in discover_agents(AGENTS)}
    shell = next(tool for tool in agents["ops-agent"].tools if tool.name == "shell")

    assert shell.settings["allow"] == ["git", "ls", "cat", "wc", "head", "tail", "find"]
    assert shell.settings["timeout"] == 30


async def test_the_example_ops_agent_actually_refuses_what_is_not_allowed():
    registry = await Registry.create(
        AGENTS,
        exclude_agents=[
            "draft-agent", "sales-agent", "inventory-agent", "writer-agent", "web-agent",
        ],
    )
    try:
        ops = registry.agent_for("agent__ops-agent")
        box = registry.toolbox_for(ops)
        names = {schema["function"]["name"] for schema in box.schemas()}
        assert {"shell_run", "shell_which", "shell_policy"} <= names

        allowed = await box.call("shell_run", {"command": "ls"})
        assert "exit_code" in allowed

        refused = await box.call("shell_run", {"command": "curl https://example.com"})
        assert "not in the allowed list" in refused
    finally:
        await registry.aclose()


async def test_exclude_agents_drops_the_draft():
    names = {agent.name for agent in discover_agents(AGENTS, exclude_agents=["draft-agent"])}
    assert names == {
        "sales-agent",
        "inventory-agent",
        "writer-agent",
        "ticket-opener",
        "answer-archiver",
        "web-agent",
        "browser-agent",
        "vision-agent",
        "ops-agent",
    }


async def test_frontmatter_is_wired_as_documented():
    agents = {agent.name: agent for agent in discover_agents(AGENTS)}

    sales = agents["sales-agent"]
    assert (sales.provider, sales.effort, sales.max_iterations) == ("anthropic", "low", 15)
    assert sales.mcp == []

    inventory = agents["inventory-agent"]
    # Two servers are declared; only the enabled one is started.
    assert [server.name for server in inventory.mcp] == ["warehouse", "supplier-api"]
    assert [server.name for server in inventory.enabled_mcp_servers] == ["warehouse"]

    warehouse = inventory.mcp[0]
    assert warehouse.enable is True
    assert warehouse.command == sys.executable  # ${PYTHON} expanded
    assert warehouse.args == ["server.py"]
    assert warehouse.exclude == ["purge_warehouse"]

    parked = inventory.mcp[1]
    assert parked.enable is False
    assert parked.transport == "streamable_http"

    # writer-agent sets effort and a token cap, and takes the default for the rest.
    writer = agents["writer-agent"]
    assert writer.effort == "medium"
    assert writer.max_output_tokens == 20000
    assert writer.max_iterations == 100  # default

    # draft-agent declares only the four mandatory keys, so every optional one defaults.
    draft = agents["draft-agent"]
    assert (draft.effort, draft.max_iterations, draft.max_output_tokens) == ("medium", 100, 4096)
    assert (draft.base_url, draft.api_key, draft.mcp) == ("", "", [])


async def test_inventory_agent_mcp_server_starts_with_the_destructive_tool_filtered():
    registry = await Registry.create(AGENTS, exclude_agents=["draft-agent", "web-agent", "ops-agent"])
    try:
        inventory = registry.agent_for("agent__inventory-agent")
        names = {
            schema["function"]["name"]
            for schema in registry.toolbox_for(inventory).schemas()
        }

        assert {"list_skus", "check_stock"} <= names
        assert "purge_warehouse" not in names, "exclude: in AGENT.md must hide it"
        assert {"file_list", "file_read", "file_run"} <= names

        toolbox = registry.toolbox_for(inventory)
        low_stock = await toolbox.call("check_stock", {"sku": "ATL-LITE-002"})
        assert '"needs_reorder": true' in low_stock.lower()
        assert "ATL-PRO-001" in await toolbox.call("list_skus", {})
    finally:
        await registry.aclose()


async def test_sales_agent_script_runs_through_its_file_tool():
    registry = await Registry.create(AGENTS, exclude_agents=["draft-agent", "web-agent", "ops-agent"])
    try:
        sales = registry.agent_for("agent__sales-agent")
        output = await registry.toolbox_for(sales).call(
            "file_run", {"script": "query_sales.py", "args": ["emea"]}
        )
        assert "exit code: 0" in output
        assert "4480000" in output.replace(",", "")
    finally:
        await registry.aclose()


def test_query_sales_script_contract():
    """The script the AGENT.md instructions depend on."""
    script = AGENTS / "sales-agent" / "query_sales.py"

    one = subprocess.run(
        [sys.executable, str(script), "emea"], capture_output=True, text=True, timeout=30
    )
    assert one.returncode == 0
    assert json.loads(one.stdout)["region"] == "emea"

    every = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=30
    )
    assert set(json.loads(every.stdout)["regions"]) == {"emea", "apac", "namer"}

    unknown = subprocess.run(
        [sys.executable, str(script), "moon"], capture_output=True, text=True, timeout=30
    )
    assert unknown.returncode == 1
    assert "available" in json.loads(unknown.stdout)


@pytest.mark.parametrize(
    "script",
    [
        "01_quickstart.py",
        "02_custom_instructions.py",
        "03_slack_bot.py",
        "04_embed_programmatically.py",
        "05_offline_walkthrough.py",
        "06_web_research.py",
    ],
)
def test_example_scripts_compile(script):
    path = EXAMPLES / script
    assert path.is_file(), f"{script} is referenced by the README but missing"
    compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_offline_walkthrough_runs_with_no_credentials():
    """Example 05 must work with no API key — it is the free entry point."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.endswith(("_API_KEY", "_TOKEN"))
    }
    env["PYTHON"] = sys.executable

    result = subprocess.run(
        [sys.executable, str(EXAMPLES / "05_offline_walkthrough.py")],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=EXAMPLES.parent,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "orchestrator turns : 3" in result.stdout
    # Proves the real subprocess and the real MCP server both took part.
    assert "4,480,000" in result.stdout
    assert "reorder" in result.stdout.lower()
    for agent in ("sales-agent", "inventory-agent", "writer-agent"):
        assert agent in result.stdout

    # Both script phases ran, in order, around the orchestrator.
    assert "script agents run  : ticket-opener, answer-archiver" in result.stdout
    assert result.stdout.index("Answer:") < result.stdout.index("Archived as")


async def test_the_example_browser_agent_gets_the_vision_tools():
    """`vision: true` plus a model that can see is what unlocks the three."""
    registry = await Registry.create(AGENTS, exclude_agents=["draft-agent", "web-agent"])
    try:
        toolbox = registry.toolbox_for(registry.agent_for("agent__browser-agent"))
        names = {schema["function"]["name"] for schema in toolbox.schemas()}

        assert toolbox.vision is True
        assert {"browser_screenshot", "browser_click_at", "browser_type"} <= names
    finally:
        await registry.aclose()


async def test_a_text_only_model_loses_the_vision_tools_but_keeps_the_rest(tmp_path):
    """The end-to-end gate: `supports_vision` says no, so the three are never offered."""
    directory = tmp_path / "blind-agent"
    directory.mkdir()
    (directory / "AGENT.md").write_text(
        "---\n"
        "name: blind-agent\n"
        "description: An agent whose model cannot see.\n"
        "provider: deepseek\n"
        "model: deepseek-chat\n"
        "tools:\n"
        "  browser:\n"
        "    vision: true\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )

    registry = await Registry.create(tmp_path)
    try:
        toolbox = registry.toolbox_for(registry.agent_for("agent__blind-agent"))
        names = {schema["function"]["name"] for schema in toolbox.schemas()}

        assert toolbox.vision is False
        assert not ({"browser_screenshot", "browser_click_at", "browser_type"} & names)
        # The rest of the browser toolset is unaffected — it never needed to see.
        assert {"browser_open", "browser_elements", "browser_click"} <= names
    finally:
        await registry.aclose()


async def test_the_example_vision_agent_attaches_the_debugger_eagerly():
    """The difference between example 07 and 08: a bar from the first tab, not the first look."""
    agents = {agent.name: agent for agent in discover_agents(AGENTS)}
    browser = next(t for t in agents["vision-agent"].tools if t.name == "browser")

    assert browser.settings["vision"] is True
    assert browser.settings["attach_debugger"] is True

    # browser-agent works from page structure, so it must not raise the bar on every tab.
    other = next(t for t in agents["browser-agent"].tools if t.name == "browser")
    assert "attach_debugger" not in other.settings


async def test_the_example_vision_agent_is_given_only_the_visual_tools():
    """Its whole premise is the picture.

    Leaving the DOM-reading tools in gives it a second, contradictory way to work, and on a
    canvas app they return a toolbar and nothing else — which reads as "keep looking" and is
    how a five-step task becomes thirty.
    """
    registry = await Registry.create(AGENTS, exclude_agents=["draft-agent", "web-agent"])
    try:
        names = {
            schema["function"]["name"]
            for schema in registry.toolbox_for(
                registry.agent_for("agent__vision-agent")
            ).schemas()
        }

        assert {"browser_screenshot", "browser_click_at", "browser_type"} <= names
        # No second way to work, and no files to wander into.
        assert not (names & {"browser_text", "browser_elements", "browser_click", "browser_fill"})
        assert not {name for name in names if name.startswith("file_")}
    finally:
        await registry.aclose()


async def test_the_example_browser_agent_still_has_both_modes():
    """The narrowing is vision-agent's specialisation, not a change to the shared toolset."""
    registry = await Registry.create(AGENTS, exclude_agents=["draft-agent", "web-agent"])
    try:
        names = {
            schema["function"]["name"]
            for schema in registry.toolbox_for(
                registry.agent_for("agent__browser-agent")
            ).schemas()
        }
        assert {"browser_elements", "browser_text", "browser_screenshot"} <= names
    finally:
        await registry.aclose()


async def test_the_example_vision_agent_narrates_and_keeps_its_screenshots():
    agents = {agent.name: agent for agent in discover_agents(AGENTS)}
    browser = next(t for t in agents["vision-agent"].tools if t.name == "browser")

    assert browser.settings["show_activity"] is True
    assert browser.settings["screenshot_path"] == "screenshots"


async def test_the_example_vision_agent_saves_inside_its_own_folder():
    """A relative screenshot_path must not escape the agent's directory."""
    from stark.orchestration import build_toolsets

    agents = {agent.name: agent for agent in discover_agents(AGENTS)}
    agent = agents["vision-agent"]
    tools = next(
        instance
        for instance, _ in build_toolsets(agent.enabled_tools, agent.path, "test")
        if hasattr(instance, "screenshot_path")
    )

    # Inside the agent's own directory, not somewhere a relative path could escape to.
    assert tools.screenshot_path == Path(agent.path) / "screenshots"
    assert Path(agent.path) in tools.screenshot_path.parents


def test_the_visual_example_forbids_reconnaissance_delegations():
    """The failure that kept looking like a scrolling bug.

    A general-purpose orchestrator handed a complex document task reaches for reconnaissance
    first — "describe the structure so I understand it, do not change anything yet". No
    instruction inside vision-agent can override that, because it arrives as the task itself,
    and the agent spends its whole budget surveying. Guarded here because it recurred across
    several runs and is invisible in any other test.
    """
    source = (EXAMPLES / "08_visual_browsing.py").read_text(encoding="utf-8")
    instructions = source.split("instructions=(")[1].split("),")[0].lower()

    assert "never delegate a reconnaissance step" in instructions
    for forbidden in ("describe", "survey", "do not change"):
        assert forbidden in instructions, f"the brief must rule out '{forbidden}' steps"
    assert "in the user's own words" in instructions


def test_the_vision_agent_bounds_a_reporting_task_to_one_screenshot():
    """Defence in depth: the agent has to survive being *told* to go and look."""
    agents = {agent.name: agent for agent in discover_agents(AGENTS)}
    body = agents["vision-agent"].instructions.lower()

    assert "screenshot, then act" in body
    # It must not simply refuse a reporting task — it answers from one screen and stops.
    assert "one** screenshot" in body or "one screenshot" in body


def test_the_vision_agent_loop_continues_by_scrolling():
    """A step covering a whole table is not done when the visible rows are done.

    Guarded because I removed this clause once while editing the loop for speed, and the
    agent then filled the rows on screen and stopped — leaving most of the table untouched
    with no error to show for it.
    """
    agents = {agent.name: agent for agent in discover_agents(AGENTS)}
    body = agents["vision-agent"].instructions

    assert "Not finished? Scroll" in body
    assert "atEnd" in body, "the loop needs a completion signal, not a guess"


def test_the_vision_agent_bulk_check_never_halts():
    """The efficiency check must be a thought, not a stop.

    "Three in a row is the limit" with no escape stalled the agent on work that genuinely has
    no bulk form — it stopped hunting for a shortcut that did not exist.
    """
    body = {a.name: a for a in discover_agents(AGENTS)}["vision-agent"].instructions
    assert "carry on one at a time" in body
    assert "never stop" in body


def test_the_vision_agent_uses_the_portable_shortcut_modifier():
    """`ctrl+z` is a no-op on macOS, where the shortcut key is Command."""
    body = {a.name: a for a in discover_agents(AGENTS)}["vision-agent"].instructions

    lines = body.splitlines()
    for index, line in enumerate(lines):
        if "`ctrl+" not in line:
            continue
        # Allowed only while explaining *why* ctrl is wrong — and that explanation wraps, so
        # look at the sentence around it rather than the one line.
        context = " ".join(lines[max(0, index - 2): index + 3])
        assert "macOS" in context or "Mac" in context, (
            f"instruction still tells it to press ctrl: {line}"
        )
