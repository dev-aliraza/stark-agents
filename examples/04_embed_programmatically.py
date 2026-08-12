#!/usr/bin/env python3
"""Use the orchestrator directly, with no listener.

This is the shape you want inside a web request handler, a scheduled job, or a test:
build the registry once, then answer many queries against it. MCP servers start on the
first line and stay warm for every question, so you pay process-spawn cost once.

    export ANTHROPIC_API_KEY=...
    python examples/04_embed_programmatically.py

The `CollectingSink` below is the extension point: implement `ResponseSink` and you can
route output anywhere Stark has no built-in listener for — a websocket, an SSE response,
a queue, a log.
"""

import asyncio
import os
import sys

import stark
from stark import Message, Orchestrator, Registry, ResponseSink

# See 01_quickstart.py: pin inventory-agent's MCP server to this interpreter.
os.environ.setdefault("PYTHON", sys.executable)

QUESTIONS = [
    "What were EMEA sales in Q2?",
    "Is ATL-LITE-002 in stock, and does it need reordering?",
    "In two sentences for an exec, how did APAC do across Q1 and Q2?",
]

INSTRUCTIONS = (
    "You coordinate a commercial-operations team. Gather facts from your agents before "
    "answering, then answer the user directly in plain language."
)


class CollectingSink(ResponseSink):
    """Buffers the answer instead of printing it, and records progress events."""

    def __init__(self) -> None:
        self.text = ""
        self.events: list[tuple[str, str]] = []
        self.failure: str | None = None

    async def chunk(self, text: str) -> None:
        # Called for each streamed slice. Forward it to your transport here.
        self.text += text

    async def event(self, kind: str, detail: str) -> None:
        # kind is one of: agent_start, agent_end, agent_error, tool
        self.events.append((kind, detail))

    async def final(self, text: str) -> None:
        self.text = text

    async def error(self, text: str) -> None:
        self.failure = text


async def main() -> None:
    # Step 1 — discover agents and bring their MCP servers up. Do this once.
    registry = await Registry.create("examples/agents", exclude_agents=["draft-agent"])

    print(f"Loaded {len(registry.agents)} agents:")
    print(registry.roster())

    orchestrator = Orchestrator(
        registry,
        instructions=INSTRUCTIONS,
        model=stark.orchestrator_model(),  # reads STARK_MODEL / STARK_PROVIDER / ...
    )

    try:
        total_cost = 0.0
        for question in QUESTIONS:
            print(f"\n{'─' * 72}\nQ: {question}")

            sink = CollectingSink()
            result = await orchestrator.handle(Message(text=question), sink)

            if result.error:
                print(f"failed: {result.error}")
                continue

            print(f"A: {result.output}")

            for agent_result in result.agent_results:
                status = agent_result.error or f"{agent_result.iterations} iteration(s)"
                print(f"   via {agent_result.agent} ({status})")

            print(f"   {result.iterations} orchestrator turn(s), ${result.cost:.4f}")
            total_cost += result.cost

        print(f"\n{'─' * 72}\nTotal: ${total_cost:.4f}")
    finally:
        # Shut the MCP servers down. This must run in the same task that created the
        # registry, which is why it is a try/finally rather than a separate callback.
        await registry.aclose()


if __name__ == "__main__":
    asyncio.run(main())
