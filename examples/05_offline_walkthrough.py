#!/usr/bin/env python3
"""See the whole orchestration flow with no API key and no cost.

Only the model is faked. Everything else is real: agents are discovered from
`examples/agents`, the sales agent genuinely executes `query_sales.py`, and the inventory
agent genuinely calls a live MCP server over stdio. That makes this the fastest way to
understand what Stark does — and a useful smoke test for a new agent folder.

    python examples/05_offline_walkthrough.py

The flow is:

    before phase ──▶ ticket-opener       ──▶ runs open_ticket.py, no model at all
                     (triggerRule matched, send_output posts it to the user)
                        │
    orchestrator ──┬─▶ sales-agent       ──▶ runs query_sales.py      (real subprocess)
                   └─▶ inventory-agent   ──▶ calls check_stock        (real MCP server)
                        │
    orchestrator ───────┴─▶ writer-agent ──▶ turns the facts into prose
                        │
    orchestrator ───────┴─▶ final answer
                        │
    after phase ────────┴─▶ answer-archiver ──▶ files the answer it was just handed
"""

import asyncio
import json
import os
import sys

import stark
from stark import (
    TRIGGER_POINT_AFTER,
    TRIGGER_POINT_BEFORE,
    Message,
    Orchestrator,
    Registry,
    ResponseSink,
    RunResult,
    ScriptPhase,
    stop_requested,
)
from stark.llm import LLMClient
from stark.types import Completion, ToolCall

# inventory-agent declares `command: ${PYTHON:-python3}` for its MCP server. Point that at
# the interpreter running this script, so the server starts on the environment that
# actually has the `mcp` package installed.
os.environ.setdefault("PYTHON", sys.executable)

# --------------------------------------------------------------------------------------
# A fake model. It inspects each request and replays a scripted decision, so the
# orchestration loop, tool execution and MCP calls all run for real.
# --------------------------------------------------------------------------------------


def tool_call(call_id: str, name: str, **arguments) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


async def fake_model(**kwargs) -> Completion:
    messages = kwargs["messages"]
    system = messages[0]["content"]
    results = [message for message in messages if message["role"] == "tool"]

    # ---- the orchestrator: its system prompt carries the agent roster ----------------
    if "Available agents" in system:
        if not results:
            # Two independent facts, so ask for both in one turn: they run in parallel.
            return Completion(
                content="Let me pull the figures and the stock position.",
                tool_calls=[
                    tool_call("c1", "agent__sales-agent", task="What were EMEA sales in Q2?"),
                    tool_call(
                        "c2",
                        "agent__inventory-agent",
                        task="Is ATL-LITE-002 in stock and does it need reordering?",
                    ),
                ],
            )
        if len(results) == 2:
            # Facts are in. Hand them to the writer agent through `context`.
            facts = "\n".join(str(message["content"]) for message in results)
            return Completion(
                tool_calls=[
                    tool_call(
                        "c3",
                        "agent__writer-agent",
                        task="Write two sentences for an exec audience.",
                        context=facts,
                    )
                ],
            )
        return Completion(content=str(results[-1]["content"]))

    # ---- the specialist agents: identified by their own directory in the prompt ------
    if "sales-agent" in system:
        if not results:
            return Completion(
                tool_calls=[
                    tool_call("t1", "file_run", script="query_sales.py", args=["emea"])
                ]
            )
        figures = json.loads(_stdout(str(results[-1]["content"])))
        return Completion(
            content=(
                f"EMEA Q2 sales were ${figures['q2_usd']:,} "
                f"(Q1 was ${figures['q1_usd']:,}); top product {figures['top_product']}."
            )
        )

    if "inventory-agent" in system:
        if not results:
            return Completion(
                tool_calls=[tool_call("t2", "check_stock", sku="ATL-LITE-002")]
            )
        return Completion(content=f"Stock result: {results[-1]['content']}")

    if "writer-agent" in system:
        context = messages[1]["content"]
        sales = next(
            (line for line in context.splitlines() if "EMEA Q2" in line), "Sales held steady."
        )
        return Completion(
            content=(
                f"{sales.strip()} Meanwhile ATL-LITE-002 has fallen below its reorder "
                "threshold and should be restocked before the next promotion."
            )
        )

    return Completion(content="(unscripted request)")


def _stdout(tool_output: str) -> str:
    """Pull the stdout section out of a `file_run` result."""
    marker = "stdout:\n"
    start = tool_output.index(marker) + len(marker)
    return tool_output[start:].split("\n\nstderr:")[0]


# --------------------------------------------------------------------------------------
# A sink that narrates what happens.
# --------------------------------------------------------------------------------------


class NarratingSink(ResponseSink):
    async def chunk(self, text: str) -> None:
        print(text, end="", flush=True)

    async def message(self, text: str) -> None:
        # A script agent with send_output: true delivers through here.
        print(f"\n\n[script output]\n{text}\n", flush=True)

    async def event(self, kind: str, detail: str, key: str | None = None) -> None:
        marker = {"agent_start": "→", "agent_end": "✓", "agent_error": "✗", "tool": "·"}
        print(f"\n   {marker.get(kind, '·')} {detail}", flush=True)

    async def final(self, text: str) -> None:
        print(f"\n\nAnswer: {text}\n")

    async def error(self, text: str) -> None:
        print(f"\n\nFailed: {text}\n")


async def main() -> None:
    # Swap in the fake model. `LLMClient.complete` is the single call path for both the
    # orchestrator and every agent, so this is the only seam needed.
    LLMClient.complete = staticmethod(fake_model)

    # The web and browser agents are excluded so this stays genuinely offline: the point
    # of this example is that nothing reaches out, and browser-agent would also want an
    # extension connected before it could do anything.
    registry = await Registry.create(
        "examples/agents",
        exclude_agents=["draft-agent", "web-agent", "browser-agent", "ops-agent"],
    )
    print(f"\nDiscovered {len(registry.agents)} agents:\n{registry.roster()}\n")

    for agent in registry.llm_agents:
        tools = [
            schema["function"]["name"] for schema in registry.toolbox_for(agent).schemas()
        ]
        print(f"  {agent.name} tools: {', '.join(tools)}")
    for agent in registry.script_agents:
        when = agent.trigger_point or "only when delegated to"
        print(f"  {agent.name} runs {agent.script} {when} (no model)")

    orchestrator = Orchestrator(registry, "You are a commercial-ops coordinator.", stark.orchestrator_model())

    # The ===== marker is what ticket-opener's triggerRule looks for.
    question = "===== How did EMEA do in Q2, and should we reorder ATL-LITE-002? ====="
    print(f"\n{'─' * 72}\nQ: {question}")

    message = Message(text=question, user="cli")
    sink = NarratingSink()

    try:
        # Step one: script agents with triggerPoint: before_orchestrator, in priority bands.
        before_phase = ScriptPhase(
            registry.script_agents_before, registry.script_runners(), TRIGGER_POINT_BEFORE
        )
        before = await before_phase.run(message, sink)

        # A script can end the query here by returning stop_execution: true. Nothing below
        # runs, but the response is still closed out.
        halt = stop_requested(before)
        if halt is not None:
            await sink.final("")
            result = RunResult(script_results=list(before), stopped_by=halt.agent)
        else:
            # Step two: the LLM orchestrator, given what those scripts produced.
            result = await orchestrator.handle(message, sink, before)

        # Step three: script agents with triggerPoint: after_orchestrator. They see every
        # earlier result and the answer itself. This is what runtime.py does for you.
        after_phase = ScriptPhase(
            registry.script_agents_after, registry.script_runners(), TRIGGER_POINT_AFTER
        )
        after = [] if result.stopped else await after_phase.run(
            message, sink, result.script_results, result.output
        )
        result.script_results.extend(after)
        if after:
            await sink.settle()
    finally:
        await registry.aclose()

    print(f"{'─' * 72}")
    if result.stopped:
        print(f"stopped by         : {result.stopped_by} (stop_execution)")
    print(f"script agents run  : {', '.join(item.agent for item in result.script_results)}")
    print(f"orchestrator turns : {result.iterations}")
    print(f"agents used        : {', '.join(item.agent for item in result.agent_results)}")
    for item in result.agent_results:
        print(f"  {item.agent}: {item.iterations} iteration(s) — {item.output[:60]}")


if __name__ == "__main__":
    asyncio.run(main())
