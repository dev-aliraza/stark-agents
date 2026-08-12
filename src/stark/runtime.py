from __future__ import annotations

import asyncio
import os

from .config import Config
from .listeners import CLI, Message, ResponseSink, build_listener, validate_listener
from .logger import logger
from .orchestration import Orchestrator, Registry, ScriptPhase
from .types import (
    DEFAULT_EFFORT,
    DEFAULT_INSTRUCTIONS,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    EFFORT_LEVELS,
    ModelConfig,
    RunResult,
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s is not an integer; using %s", name, default)
        return default
    return value if value > 0 else default


def _env_effort() -> str:
    raw = (os.environ.get("STARK_EFFORT") or DEFAULT_EFFORT).lower()
    if raw not in EFFORT_LEVELS:
        logger.warning("STARK_EFFORT '%s' is not a known level; using %s", raw, DEFAULT_EFFORT)
        return DEFAULT_EFFORT
    return raw


def orchestrator_model() -> ModelConfig:
    """Model settings for the orchestration loop, read from the environment.

    `stark.run()` has a fixed signature, so the orchestrator's own model is
    configured here. Anthropic is the first-class default.
    """
    return ModelConfig(
        provider=(os.environ.get("STARK_PROVIDER") or DEFAULT_PROVIDER).lower(),
        model=os.environ.get("STARK_MODEL") or DEFAULT_MODEL,
        effort=_env_effort(),
        max_iterations=_env_int("STARK_MAX_ITERATIONS", DEFAULT_MAX_ITERATIONS),
        max_output_tokens=_env_int("STARK_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS),
        base_url=os.environ.get("STARK_BASE_URL") or "",
        api_key=os.environ.get("STARK_API_KEY") or "",
    )


async def run_async(
    agents: str = "./agents",
    listener: str = CLI,
    exclude_agents: list[str] | None = None,
    instructions: str = DEFAULT_INSTRUCTIONS,
    config: Config | dict | None = None,
) -> None:
    """The async form of `stark.run()`, for embedding in an existing event loop."""
    model = orchestrator_model()

    # Coerce before any startup work, so a bad key fails immediately.
    settings = Config.coerce(config)

    # Fail on a bad listener before starting anything expensive.
    kind = validate_listener(listener)

    # Step 1 — discover agents, bring MCP servers up, import script agents.
    registry = await Registry.create(agents, exclude_agents or [])
    script_phase = ScriptPhase(registry.script_agents, registry.script_runners())
    orchestrator = Orchestrator(registry, instructions, model)

    if registry.has_llm_agents:
        logger.info(
            "Ready: %d llm agent(s) on %s/%s, %d script agent(s)",
            len(registry.llm_agents),
            model.provider,
            model.model,
            script_phase.agent_count,
        )
    else:
        logger.info(
            "Ready: %d script agent(s), no llm agents — no model will be called",
            script_phase.agent_count,
        )

    async def handle(message: Message, sink: ResponseSink) -> RunResult:
        # Step 3a — deterministic script agents, in priority bands.
        script_results = await script_phase.run(message, sink)

        # Step 3b — the LLM orchestrator, but only if it has somewhere to route.
        if registry.has_llm_agents:
            return await orchestrator.handle(message, sink, script_results)

        # Nothing to reason with. Close the response out silently: the script steps
        # already told the user what happened.
        await sink.final("")
        return RunResult(script_results=script_results, orchestrator_ran=False)

    options = {"roster": registry.roster()} if kind == CLI else {}
    active = build_listener(kind, handle, config=settings, **options)

    # Step 2 — hold the process open on the listener. The registry is closed in the
    # same task that opened it, which is what the MCP transports require.
    try:
        await active.start()
    finally:
        await active.stop()
        await registry.aclose()


def run(
    agents: str = "./agents",
    listener: str = CLI,
    exclude_agents: list[str] | None = None,
    instructions: str = DEFAULT_INSTRUCTIONS,
    config: Config | dict | None = None,
) -> None:
    """Discover agents, start a listener, and serve queries until interrupted.

    Args:
        agents: Directory containing one subdirectory per agent, each with an
            `AGENT.md` at its root. Directories without one are skipped.
        listener: `"cli"` for an interactive terminal prompt, or `"slack"` for a
            Socket Mode listener on mentions and DMs.
        exclude_agents: Directory names inside `agents` to ignore during discovery.
        instructions: The master system prompt governing the orchestration loop.
        config: Presentation settings, as a `Config` or a plain nested dict. Currently
            covers the Slack progress message:

                config={"slack": {"running_emoji": ":hourglass:",
                                  "done_emoji": ":white_check_mark:",
                                  "failed_emoji": ":x:"}}
    """
    try:
        asyncio.run(
            run_async(
                agents=agents,
                listener=listener,
                exclude_agents=exclude_agents,
                instructions=instructions,
                config=config,
            )
        )
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting down")
