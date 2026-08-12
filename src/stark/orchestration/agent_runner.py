from __future__ import annotations

import asyncio
from typing import Any

from ..listeners.base import ResponseSink
from ..llm import LLMClient
from ..logger import get_logger
from ..types import AgentConfig, AgentResult, ToolCall
from .registry import ToolBox

logger = get_logger("agent")


class AgentRunner:
    """Runs one agent's own tool-calling loop over a single delegated task.

    Each run is independent: the agent sees its `AGENT.md` body as its system prompt
    plus the task it was handed, never the orchestrator's conversation. Its own
    `max_iterations` and `max_output_tokens` bound the loop.
    """

    def __init__(self, config: AgentConfig, toolbox: ToolBox):
        self.config = config
        self.toolbox = toolbox

    def _system_prompt(self) -> str:
        sections = [self.config.instructions.strip()] if self.config.instructions.strip() else []
        sections.append(
            "## Your workspace\n"
            f"Your agent directory is `{self.config.path}`. The `workspace_list`, "
            "`workspace_read` and `workspace_run` tools operate inside it — use "
            "`workspace_run` when your instructions tell you to run one of your scripts."
        )
        sections.append(
            "## Reporting back\n"
            "You were given one task by an orchestrator. Complete it with the tools "
            "available, then reply with the result itself — the orchestrator sees only "
            "your final message, so include the findings rather than describing what you "
            "did. If you could not complete the task, say so plainly and explain why."
        )
        return "\n\n".join(sections)

    async def run(self, task: str, context: str, sink: ResponseSink) -> AgentResult:
        result = AgentResult(agent=self.config.name, task=task)

        user_content = task if not context.strip() else f"{task}\n\n## Context\n{context.strip()}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": user_content},
        ]
        tools = self.toolbox.schemas()

        await sink.event("agent_start", f"{self.config.name}: {task}")

        while result.iterations < self.config.max_iterations:
            result.iterations += 1

            try:
                completion = await LLMClient.complete(
                    provider=self.config.provider,
                    model=self.config.model,
                    messages=messages,
                    tools=tools,
                    effort=self.config.effort,
                    max_output_tokens=self.config.max_output_tokens,
                    base_url=self.config.base_url,
                    api_key=self.config.api_key,
                )
            except Exception as exc:
                result.error = f"{type(exc).__name__}: {exc}"
                logger.error("Agent '%s' model call failed: %s", self.config.name, exc)
                await sink.event("agent_error", f"{self.config.name}: {result.error}")
                return result

            result.cost += completion.cost
            messages.append(completion.as_message())

            if not completion.tool_calls:
                result.output = completion.content.strip()
                await sink.event("agent_end", f"{self.config.name} finished")
                return result

            responses = await self._run_tools(completion.tool_calls, sink)
            messages.extend(responses)

        result.max_iterations_reached = True
        result.output = self._last_text(messages)
        logger.warning(
            "Agent '%s' hit its %d-iteration limit",
            self.config.name,
            self.config.max_iterations,
        )
        await sink.event("agent_end", f"{self.config.name} stopped at its iteration limit")
        return result

    async def _run_tools(
        self, calls: list[ToolCall], sink: ResponseSink
    ) -> list[dict[str, Any]]:
        """Execute every tool the model asked for, concurrently."""

        async def execute(call: ToolCall) -> dict[str, Any]:
            await sink.event("tool", f"{self.config.name} → {call.name}")
            try:
                content = await self.toolbox.call(call.name, call.parsed_arguments())
            except Exception as exc:
                logger.error("Agent '%s' tool '%s' failed: %s", self.config.name, call.name, exc)
                content = f"[error] {call.name} failed: {exc}"
            return {"role": "tool", "tool_call_id": call.id, "content": content}

        return list(await asyncio.gather(*(execute(call) for call in calls)))

    @staticmethod
    def _last_text(messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages):
            if message.get("role") == "assistant" and message.get("content"):
                return str(message["content"]).strip()
        return ""
