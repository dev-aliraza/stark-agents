from __future__ import annotations

import asyncio
from typing import Any

from ..listeners.base import ResponseSink
from ..llm import LLMClient
from ..logger import get_logger
from ..types import AgentConfig, AgentResult, ToolCall, ToolImage
from ..vision import image_message, prune_images
from .registry import ToolBox
from .tool_output import split_result

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

    # The order they read best in, not the order they are defined in.
    _FILE_TOOLS = ("file_list", "file_read", "file_write", "file_delete", "file_run")

    def _file_section(self) -> str:
        """Describe the file tools this agent actually has, or say nothing.

        Naming tools an agent cannot call is worse than saying nothing: it spends turns
        reaching for them, and an agent given a narrow toolset on purpose is precisely the one
        least able to absorb the distraction. So this follows `enable` and `exclude` rather
        than assuming the default five.
        """
        available = [tool for tool in self._FILE_TOOLS if self.toolbox.offers(tool)]
        if not available:
            return ""

        listed = ", ".join(f"`{tool}`" for tool in available)
        section = (
            "## Your files\n"
            f"Your agent directory is `{self.config.path}`. The {listed} "
            f"{'tools' if len(available) > 1 else 'tool'} operate inside it, and nowhere else"
        )
        if "file_run" in available:
            section += " — use `file_run` when your instructions tell you to run one of your scripts"
        section += "."

        if {"file_write", "file_delete"} & set(available):
            section += (
                "\nWriting and deleting change real files. Create a file when you have "
                "something worth keeping, and only delete something you created or were "
                "explicitly told to remove."
            )
        return section

    def _system_prompt(self) -> str:
        sections = [self.config.instructions.strip()] if self.config.instructions.strip() else []
        files = self._file_section()
        if files:
            sections.append(files)
        sections.append(
            "## Reporting back\n"
            "You were given one task by an orchestrator. Complete it with the tools "
            "available, then reply with the result itself — the orchestrator sees only "
            "your final message, so include the findings rather than describing what you "
            "did. If you could not complete the task, say so plainly and explain why."
        )
        return "\n\n".join(sections)

    async def run(
        self,
        task: str,
        context: str,
        sink: ResponseSink,
        key: str | None = None,
    ) -> AgentResult:
        """Run one delegated task.

        `key` identifies this delegation so progress events for it can be correlated;
        the orchestrator passes the tool-call id, which is unique per turn.
        """
        result = AgentResult(agent=self.config.name, task=task)
        key = key or f"agent:{self.config.name}"

        user_content = task if not context.strip() else f"{task}\n\n## Context\n{context.strip()}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": user_content},
        ]
        tools = self.toolbox.schemas()

        await sink.event("agent_start", f"{self.config.name}: {task}", key=key)

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
                await sink.event("agent_error", f"{self.config.name}: {result.error}", key=key)
                return result

            result.cost += completion.cost
            messages.append(completion.as_message())

            if not completion.tool_calls:
                result.output = completion.content.strip()
                await sink.event("agent_end", f"{self.config.name} finished", key=key)
                return result

            messages.extend(await self._run_tools(completion.tool_calls, sink, key))
            # Older screenshots become stubs here. Every tool result is re-sent on every
            # later turn, so without this a ten-step browsing task pays for its first
            # screenshot ten times over.
            prune_images(messages)

        result.max_iterations_reached = True
        result.output = self._last_text(messages)
        logger.warning(
            "Agent '%s' hit its %d-iteration limit",
            self.config.name,
            self.config.max_iterations,
        )
        await sink.event(
            "agent_end", f"{self.config.name} stopped at its iteration limit", key=key
        )
        return result

    async def _run_tools(
        self, calls: list[ToolCall], sink: ResponseSink, agent_key: str
    ) -> list[dict[str, Any]]:
        """Execute every tool the model asked for, concurrently.

        Returns the tool messages, followed by one user message carrying any images the turn
        produced. They travel separately because `role: "tool"` content must be a string on
        OpenAI — see `stark.vision` for why that decides the shape.
        """

        async def execute(call: ToolCall) -> tuple[dict[str, Any], list[ToolImage]]:
            # Namespaced by the agent so two agents running the same tool concurrently
            # never share a key.
            tool_key = f"{agent_key}:{call.id}"
            label = f"{self.config.name} → {call.name}"
            await sink.event("tool", label, key=tool_key)
            try:
                result = await self.toolbox.call(call.name, call.parsed_arguments())
            except Exception as exc:
                logger.error("Agent '%s' tool '%s' failed: %s", self.config.name, call.name, exc)
                result = f"[error] {call.name} failed: {exc}"
            await sink.event("tool_end", label, key=tool_key)

            text, images = split_result(result)
            return {"role": "tool", "tool_call_id": call.id, "content": text}, images

        outcomes = await asyncio.gather(*(execute(call) for call in calls))
        messages = [message for message, _ in outcomes]
        attached = image_message([image for _, images in outcomes for image in images])
        if attached is not None:
            messages.append(attached)
        return messages

    @staticmethod
    def _last_text(messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages):
            if message.get("role") == "assistant" and message.get("content"):
                return str(message["content"]).strip()
        return ""
