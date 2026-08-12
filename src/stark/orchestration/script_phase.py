"""The script phase: deterministic agents that run before the LLM orchestrator.

Script agents are grouped into priority bands. Bands run in descending priority order,
one after another; agents sharing a band run concurrently, on the assumption stated in
the design that same-priority agents are independent of each other.

Each band sees everything the earlier bands produced, so ordering can express a real
dependency (create the ticket, then notify about it) rather than only sequencing side
effects.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterable

from ..listeners.base import Message, ResponseSink
from ..logger import get_logger
from ..types import AgentConfig, ScriptResult
from .script_runner import ScriptRunner

logger = get_logger("scripts")


def group_into_bands(agents: Iterable[AgentConfig]) -> list[tuple[int, list[AgentConfig]]]:
    """Bucket script agents by priority, highest first.

    Within a band, agents keep a stable name order so a run is reproducible.
    """
    bands: dict[int, list[AgentConfig]] = {}
    for agent in agents:
        bands.setdefault(agent.priority, []).append(agent)

    return [
        (priority, sorted(bands[priority], key=lambda item: item.name))
        for priority in sorted(bands, reverse=True)
    ]


def trigger_values(message: Message) -> dict[str, str | None]:
    """The message fields a triggerRule may read."""
    return {
        "text": message.text,
        "user": message.user,
        "channel": message.channel,
        "thread": message.thread,
    }


class ScriptPhase:
    """Runs every script agent whose trigger matches, in priority order."""

    def __init__(self, agents: list[AgentConfig], runners: dict[str, ScriptRunner]):
        self.bands = group_into_bands(agents)
        self.runners = runners

    @property
    def agent_count(self) -> int:
        return sum(len(agents) for _, agents in self.bands)

    async def run(self, message: Message, sink: ResponseSink) -> list[ScriptResult]:
        """Execute the phase and return every result, in completion order per band."""
        if not self.bands:
            return []

        values = trigger_values(message)
        collected: list[ScriptResult] = []

        for priority, agents in self.bands:
            matched = [agent for agent in agents if self._matches(agent, values)]
            if not matched:
                continue

            logger.info(
                "Script band %s: running %s",
                priority,
                ", ".join(agent.name for agent in matched),
            )

            # Every agent in the band sees the same snapshot of prior output; results
            # from its own band are not visible to its peers.
            snapshot = list(collected)
            results = await asyncio.gather(
                *(self._run_one(agent, message, snapshot, sink) for agent in matched)
            )
            collected.extend(results)

        return collected

    def _matches(self, agent: AgentConfig, values: dict[str, str | None]) -> bool:
        matched = agent.triggered_by(values)
        if not matched:
            logger.debug(
                "Script agent '%s' skipped: triggerRule did not match (%s)",
                agent.name,
                agent.trigger_rule,
            )
        return matched

    async def _run_one(
        self,
        agent: AgentConfig,
        message: Message,
        prior: list[ScriptResult],
        sink: ResponseSink,
    ) -> ScriptResult:
        runner = self.runners.get(agent.name)
        if runner is None:
            # Registry refused to load the script; surface it rather than skip silently.
            result = ScriptResult(
                agent=agent.name,
                priority=agent.priority,
                error="script was not loaded at startup",
            )
            await sink.event("agent_error", f"{agent.name}: {result.error}", key=agent.name)
            return result

        await sink.event("agent_start", f"{agent.name} (script)", key=agent.name)

        result = await runner.run(self._payload(agent, message, prior))

        if result.error:
            # Fail-open: the phase continues and the error travels as context.
            await sink.event("agent_error", f"{agent.name}: {result.error}", key=agent.name)
            return result

        if agent.send_output and result.output.strip():
            await sink.message(result.output)
            result.sent_to_client = True

        await sink.event("agent_end", f"{agent.name} (script)", key=agent.name)
        return result

    @staticmethod
    def _payload(
        agent: AgentConfig, message: Message, prior: list[ScriptResult]
    ) -> dict[str, Any]:
        """What `run()` receives.

        A plain dict, not the Message dataclass, so a script never imports stark and can
        be unit-tested on its own.
        """
        return {
            "text": message.text,
            "user": message.user,
            "channel": message.channel,
            "thread": message.thread,
            "meta": message.meta,
            "agent": agent.name,
            "workspace": str(agent.path),
            "prior_outputs": [
                {"agent": item.agent, "output": item.output, "error": item.error}
                for item in prior
            ],
        }
