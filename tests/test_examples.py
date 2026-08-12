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
    }


async def test_the_example_script_agent_is_wired_as_documented():
    agents = {agent.name: agent for agent in discover_agents(AGENTS)}
    ticket = agents["ticket-opener"]

    assert ticket.is_script
    assert ticket.script == "open_ticket.py"
    assert (ticket.priority, ticket.send_output) == (200, True)
    assert ticket.trigger_rule is not None
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


async def test_exclude_agents_drops_the_draft():
    names = {agent.name for agent in discover_agents(AGENTS, exclude_agents=["draft-agent"])}
    assert names == {"sales-agent", "inventory-agent", "writer-agent", "ticket-opener"}


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
    registry = await Registry.create(AGENTS, exclude_agents=["draft-agent"])
    try:
        inventory = registry.agent_for("agent__inventory-agent")
        names = {
            schema["function"]["name"]
            for schema in registry.toolbox_for(inventory).schemas()
        }

        assert {"list_skus", "check_stock"} <= names
        assert "purge_warehouse" not in names, "exclude: in AGENT.md must hide it"
        assert {"workspace_list", "workspace_read", "workspace_run"} <= names

        toolbox = registry.toolbox_for(inventory)
        low_stock = await toolbox.call("check_stock", {"sku": "ATL-LITE-002"})
        assert '"needs_reorder": true' in low_stock.lower()
        assert "ATL-PRO-001" in await toolbox.call("list_skus", {})
    finally:
        await registry.aclose()


async def test_sales_agent_script_runs_through_its_workspace_tool():
    registry = await Registry.create(AGENTS, exclude_agents=["draft-agent"])
    try:
        sales = registry.agent_for("agent__sales-agent")
        output = await registry.toolbox_for(sales).call(
            "workspace_run", {"script": "query_sales.py", "args": ["emea"]}
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
