from __future__ import annotations

import asyncio
from typing import Any

from ..listeners.base import Message, ResponseSink
from ..llm import LLMClient
from ..logger import get_logger
from ..types import ModelConfig, RunResult, ToolCall
from .agent_runner import AgentRunner
from .registry import Registry

logger = get_logger("orchestrator")

_DELEGATION_RULES = """\
## Delegating

Each agent above is available as a tool. An agent runs in its own context and cannot
see this conversation, so every task you send must stand on its own.

- When a request splits into independent parts, call several agents in one turn so
  they run in parallel.
- When one agent's output feeds another, call them in sequence and pass what you
  learned in the `context` field.
- Answer directly when no agent is relevant; do not delegate for its own sake.
- Once the work is done, reply to the user yourself with the answer — they see your
  message, not the agents' raw output."""


class Orchestrator:
    """The master loop: evaluates a query against the agent roster and delegates."""

    def __init__(self, registry: Registry, instructions: str, model: ModelConfig):
        self.registry = registry
        self.instructions = instructions
        self.model = model

    def system_prompt(self) -> str:
        sections = [self.instructions.strip()] if self.instructions.strip() else []
        if self.registry.agents:
            sections.append(f"## Available agents\n{self.registry.roster()}")
            sections.append(_DELEGATION_RULES)
        else:
            sections.append(
                "No specialist agents are registered, so answer the user directly."
            )
        return "\n\n".join(sections)

    async def handle(self, message: Message, sink: ResponseSink) -> RunResult:
        """Run one query to completion, streaming the answer to the sink."""
        result = RunResult()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": message.text},
        ]
        tools = self.registry.delegation_tools()

        while result.iterations < self.model.max_iterations:
            result.iterations += 1

            try:
                completion = await LLMClient.complete(
                    provider=self.model.provider,
                    model=self.model.model,
                    messages=messages,
                    tools=tools,
                    effort=self.model.effort,
                    max_output_tokens=self.model.max_output_tokens,
                    base_url=self.model.base_url,
                    api_key=self.model.api_key,
                    on_text=sink.chunk,
                )
            except Exception as exc:
                result.error = f"{type(exc).__name__}: {exc}"
                logger.error("Orchestrator model call failed: %s", exc)
                await sink.error(result.error)
                return result

            result.cost += completion.cost
            messages.append(completion.as_message())

            if not completion.tool_calls:
                result.output = completion.content.strip()
                await sink.final(result.output)
                return result

            responses = await self._delegate(completion.tool_calls, result, sink)
            messages.extend(responses)

        result.max_iterations_reached = True
        result.output = completion.content.strip()
        logger.warning("Orchestrator hit its %d-iteration limit", self.model.max_iterations)
        await sink.final(
            result.output
            or "I reached my iteration limit before finishing. Please narrow the request."
        )
        return result

    async def _delegate(
        self,
        calls: list[ToolCall],
        result: RunResult,
        sink: ResponseSink,
    ) -> list[dict[str, Any]]:
        """Fan out every requested agent call in parallel and collect the results."""

        async def execute(call: ToolCall) -> dict[str, Any]:
            if not self.registry.is_agent_tool(call.name):
                logger.warning("Model requested unknown tool '%s'", call.name)
                return {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": f"[error] no agent named '{call.name}' is registered",
                }

            agent = self.registry.agent_for(call.name)
            arguments = call.parsed_arguments()
            task = str(arguments.get("task") or "").strip()
            context = str(arguments.get("context") or "")

            if not task:
                return {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": "[error] 'task' is required; describe what this agent should do",
                }

            runner = AgentRunner(agent, self.registry.toolbox_for(agent))
            agent_result = await runner.run(task, context, sink)
            result.agent_results.append(agent_result)
            result.cost += agent_result.cost

            return {
                "role": "tool",
                "tool_call_id": call.id,
                "content": agent_result.as_tool_content(),
            }

        return list(await asyncio.gather(*(execute(call) for call in calls)))
