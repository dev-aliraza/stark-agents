from __future__ import annotations

import asyncio
from typing import Any

from ..listeners.base import Message, ResponseSink
from ..llm import LLMClient
from ..logger import get_logger
from ..types import (
    INVOCATION_DELEGATION,
    AgentConfig,
    ModelConfig,
    RunResult,
    ScriptResult,
    ToolCall,
)
from .agent_runner import AgentRunner
from .registry import Registry
from .script_phase import stop_requested
from .script_runner import build_payload

logger = get_logger("orchestrator")

_DELEGATION_RULES = """\
## Delegating

Each agent above is available as a tool. An agent runs in its own context and cannot
see this conversation, so every task you send must stand on its own.

- When a request splits into independent parts, call several agents in one turn so
  they run in parallel.
- When one agent's output feeds another, call them in sequence and pass what you
  learned in the `context` field.
- An agent marked as a deterministic script performs a fixed action rather than
  reasoning about your request. Calling one is an action with real effects, so call it
  only when the user's request needs that action taken.
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
        # Only llm agents are delegatable, so only they belong in the prompt.
        if self.registry.has_llm_agents:
            sections.append(f"## Available agents\n{self.registry.roster()}")
            sections.append(_DELEGATION_RULES)
        else:
            sections.append(
                "No specialist agents are registered, so answer the user directly."
            )
        return "\n\n".join(sections)

    @staticmethod
    def _user_content(message: Message, script_results: list[ScriptResult]) -> str:
        """The first user turn: the query, plus anything the script phase produced."""
        if not script_results:
            return message.text

        sections = [
            message.text,
            "",
            "## Results from automated steps",
            "These ran deterministically before you, for this message.",
            "",
        ]
        sections.extend(item.as_context() for item in script_results)
        sections.append("")
        sections.append(
            "Use these results. Anything marked as already shown to the user must not be "
            "repeated — add only what is missing, and keep it short if there is nothing "
            "substantive to add."
        )
        return "\n".join(sections)

    async def handle(
        self,
        message: Message,
        sink: ResponseSink,
        script_results: list[ScriptResult] | None = None,
    ) -> RunResult:
        """Run one query to completion, streaming the answer to the sink.

        `script_results` are outcomes from the script phase that already ran. They are
        given to the model as context, labelled with whether the user has already seen
        them, so it builds on them instead of repeating them.
        """
        result = RunResult(script_results=list(script_results or []), orchestrator_ran=True)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": self._user_content(message, result.script_results)},
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

            # Only results from this turn can halt the loop; anything handed in by the
            # caller was its own to act on.
            already = len(result.script_results)
            responses = await self._delegate(completion.tool_calls, message, result, sink)
            messages.extend(responses)

            # A delegated script agent can halt the run. The tool results from this turn are
            # discarded rather than sent back for another turn — "stop" has to mean the model
            # is not consulted again, or it would answer around the halt.
            halt = stop_requested(result.script_results[already:])
            if halt is not None:
                result.stopped_by = halt.agent
                logger.info(
                    "Script agent '%s' stopped the run after %d orchestrator turn(s)",
                    halt.agent,
                    result.iterations,
                )
                await sink.final("")
                return result

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
        message: Message,
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

            # The tool-call id is unique per turn, so it keys this delegation's progress
            # even when the same agent is called twice at once.
            if agent.is_script:
                content = await self._delegate_to_script(
                    agent, task, context, message, result, sink, key=call.id
                )
                return {"role": "tool", "tool_call_id": call.id, "content": content}

            runner = AgentRunner(agent, self.registry.toolbox_for(agent))
            agent_result = await runner.run(task, context, sink, key=call.id)
            result.agent_results.append(agent_result)
            result.cost += agent_result.cost

            return {
                "role": "tool",
                "tool_call_id": call.id,
                "content": agent_result.as_tool_content(),
            }

        return list(await asyncio.gather(*(execute(call) for call in calls)))

    async def _delegate_to_script(
        self,
        agent: AgentConfig,
        task: str,
        context: str,
        message: Message,
        result: RunResult,
        sink: ResponseSink,
        key: str,
    ) -> str:
        """Run a delegated script agent and return its tool content.

        The trigger rule is not consulted: it governs the automatic run, and the model
        naming the agent is the same decision made explicitly. `send_output` still holds,
        so a script that promises to show the user its own output does so here too.
        """
        runner = self.registry.script_runner_for(agent)
        if runner is None:
            failure = f"[{agent.name} failed] script was not loaded at startup"
            await sink.event("agent_error", f"{agent.name}: {failure}", key=key)
            return failure

        await sink.event("agent_start", f"{agent.name} (script): {task}", key=key)

        script_result = await runner.run(
            build_payload(
                agent,
                message,
                invocation=INVOCATION_DELEGATION,
                prior=result.script_results,
                task=task,
                context=context,
            )
        )
        result.script_results.append(script_result)

        if script_result.error:
            await sink.event("agent_error", f"{agent.name}: {script_result.error}", key=key)
            return script_result.as_tool_content()

        if agent.send_output and script_result.output.strip():
            await sink.message(script_result.output)
            script_result.sent_to_client = True

        await sink.event("agent_end", f"{agent.name} finished", key=key)
        return script_result.as_tool_content()
