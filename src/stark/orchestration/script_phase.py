"""A script phase: deterministic agents that run on one side of the LLM orchestrator.

Two phases exist per run, selected by each agent's `triggerPoint`: one before the
orchestrator and one after it. Both work identically. A script agent with no `triggerPoint`
belongs to neither — it runs only when the orchestrator delegates to it.

Script agents are grouped into priority bands. Bands run in descending priority order,
one after another; agents sharing a band run concurrently, on the assumption stated in
the design that same-priority agents are independent of each other.

Each band sees everything the earlier bands produced, so ordering can express a real
dependency (create the ticket, then notify about it) rather than only sequencing side
effects. The after-orchestrator phase additionally sees the before-phase results and the
answer the orchestrator produced.

A script can halt the run by returning `stop_execution: true`. Later bands are skipped, and
the caller skips whatever came after this phase. Its own band still finishes: those agents
were started together, so by the time the flag is read they have already run.
"""

from __future__ import annotations

import asyncio
from typing import Iterable, Sequence

from ..listeners.base import Message, ResponseSink
from ..logger import get_logger
from ..types import TRIGGER_POINT_BEFORE, AgentConfig, ScriptResult
from .script_runner import ScriptRunner, build_payload

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


def stop_requested(results: Iterable[ScriptResult]) -> ScriptResult | None:
    """The first result asking to halt the run, if any.

    Callers use this to decide whether to keep going, so it is a function rather than a
    flag on the phase: the same question is asked of a delegated result too, which no phase
    ever sees.
    """
    for result in results:
        if result.stop_execution:
            return result
    return None


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

    def __init__(
        self,
        agents: list[AgentConfig],
        runners: dict[str, ScriptRunner],
        trigger_point: str = TRIGGER_POINT_BEFORE,
    ):
        self.bands = group_into_bands(agents)
        self.runners = runners
        self.trigger_point = trigger_point

    @property
    def agent_count(self) -> int:
        return sum(len(agents) for _, agents in self.bands)

    async def run(
        self,
        message: Message,
        sink: ResponseSink,
        prior: Sequence[ScriptResult] = (),
        orchestrator_output: str = "",
    ) -> list[ScriptResult]:
        """Execute the phase and return its own results, in completion order per band.

        `prior` is what earlier stages of the same run produced — for the
        after-orchestrator phase, the before-orchestrator results. It is visible to the
        scripts but not included in the return value, so a caller can concatenate the two
        phases without duplicating anything.

        Stops early if an agent returned `stop_execution`. Pass the results to
        `stop_requested` to find out whether that happened.
        """
        if not self.bands:
            return []

        values = trigger_values(message)
        collected: list[ScriptResult] = []

        for priority, agents in self.bands:
            matched = [agent for agent in agents if self._matches(agent, values)]
            if not matched:
                continue

            logger.info(
                "Script band %s (%s): running %s",
                priority,
                self.trigger_point,
                ", ".join(agent.name for agent in matched),
            )

            # Every agent in the band sees the same snapshot of prior output; results
            # from its own band are not visible to its peers.
            snapshot = [*prior, *collected]
            results = await asyncio.gather(
                *(
                    self._run_one(agent, message, snapshot, sink, orchestrator_output)
                    for agent in matched
                )
            )
            collected.extend(results)

            halt = stop_requested(results)
            if halt is not None:
                skipped = self._names_after(priority)
                logger.info(
                    "Script agent '%s' stopped the %s phase%s",
                    halt.agent,
                    self.trigger_point,
                    f"; skipping {', '.join(skipped)}" if skipped else "",
                )
                break

        return collected

    def _names_after(self, priority: int) -> list[str]:
        """Agents in lower bands than `priority`, for the "what was skipped" log line."""
        return [
            agent.name
            for band, agents in self.bands
            if band < priority
            for agent in agents
        ]

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
        prior: Sequence[ScriptResult],
        sink: ResponseSink,
        orchestrator_output: str,
    ) -> ScriptResult:
        runner = self.runners.get(agent.name)
        if runner is None:
            # Registry refused to load the script; surface it rather than skip silently.
            result = ScriptResult(
                agent=agent.name,
                priority=agent.priority,
                trigger_point=agent.trigger_point,
                error="script was not loaded at startup",
            )
            await sink.event("agent_error", f"{agent.name}: {result.error}", key=agent.name)
            return result

        await sink.event("agent_start", f"{agent.name} (script)", key=agent.name)

        result = await runner.run(
            build_payload(
                agent,
                message,
                prior=prior,
                orchestrator_output=orchestrator_output,
            )
        )

        if result.error:
            # Fail-open: the phase continues and the error travels as context.
            await sink.event("agent_error", f"{agent.name}: {result.error}", key=agent.name)
            return result

        if agent.send_output and result.output.strip():
            await sink.message(result.output)
            result.sent_to_client = True

        await sink.event("agent_end", f"{agent.name} (script)", key=agent.name)
        return result
