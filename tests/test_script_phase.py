"""The script phase: priority bands, accumulation, send_output routing, fail-open."""

from __future__ import annotations

import asyncio

import pytest

from stark.listeners.base import Message, ResponseSink
from stark.orchestration import Registry, ScriptPhase, group_into_bands, trigger_values
from stark.types import AgentConfig


class RecordingSink(ResponseSink):
    def __init__(self):
        self.messages: list[str] = []
        self.events: list[tuple[str, str, str | None]] = []
        self.final_text: str | None = None

    async def chunk(self, text: str) -> None:
        pass

    async def message(self, text: str) -> None:
        self.messages.append(text)

    async def event(self, kind: str, detail: str, key: str | None = None) -> None:
        self.events.append((kind, detail, key))

    async def final(self, text: str) -> None:
        self.final_text = text

    async def error(self, text: str) -> None:
        pass


def write_script_agent(
    root,
    name: str,
    body: str,
    *,
    priority: int | None = None,
    send_output: bool | None = None,
    trigger: str | None = None,
    trigger_point: str = "before_orchestrator",
    avoid_orchestrator: bool | None = None,
) -> None:
    """Write a script agent. This file is about the phases, so `triggerPoint` is on by
    default; pass `trigger_point=""` to omit it and get a delegation-only agent."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "handler.py").write_text(body, encoding="utf-8")

    extra = ""
    if priority is not None:
        extra += f"priority: {priority}\n"
    if send_output is not None:
        extra += f"send_output: {str(send_output).lower()}\n"
    if trigger is not None:
        extra += f"triggerRule: '{trigger}'\n"
    if trigger_point:
        extra += f"triggerPoint: {trigger_point}\n"
    if avoid_orchestrator is not None:
        extra += f"avoid_orchestrator: {str(avoid_orchestrator).lower()}\n"

    (directory / "AGENT.md").write_text(
        f"---\nname: {name}\ndescription: {name} does things.\n"
        f"type: script\nscript: handler.py\n{extra}---\n\nBody.\n",
        encoding="utf-8",
    )


def write_llm_agent(root, name: str) -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "AGENT.md").write_text(
        f"---\nname: {name}\ndescription: An llm agent.\n"
        "provider: anthropic\nmodel: claude-opus-5\n---\n\nBody.\n",
        encoding="utf-8",
    )


ECHO = "def run(message):\n    return {name!r}\n"


async def build(tmp_path) -> tuple[Registry, ScriptPhase]:
    registry = await Registry.create(tmp_path)
    return registry, ScriptPhase(registry.script_agents_before, registry.script_runners())


def message(text: str = "hello", **fields) -> Message:
    return Message(text=text, **fields)


# --- band grouping -------------------------------------------------------------------


def config(name: str, priority: int) -> AgentConfig:
    return AgentConfig(
        name=name, description="d", instructions="", path=None, type="script",
        script="handler.py", priority=priority, trigger_point="before_orchestrator",
    )


def test_bands_descend_by_priority():
    bands = group_into_bands([config("a", 100), config("b", 300), config("c", 200)])
    assert [priority for priority, _ in bands] == [300, 200, 100]


def test_agents_sharing_a_priority_form_one_band_sorted_by_name():
    bands = group_into_bands([config("z", 100), config("a", 100), config("m", 100)])
    assert len(bands) == 1
    assert [agent.name for agent in bands[0][1]] == ["a", "m", "z"]


def test_no_agents_means_no_bands():
    assert group_into_bands([]) == []


def test_trigger_values_exposes_the_four_documented_fields():
    values = trigger_values(Message(text="t", user="u", channel="c", thread="th"))
    assert values == {"text": "t", "user": "u", "channel": "c", "thread": "th"}


# --- ordering and parallelism --------------------------------------------------------


ORDER_SCRIPT = """\
import time
def run(message):
    with open({log!r}, "a") as handle:
        handle.write("{name}\\n")
    return "{name}"
"""


async def test_bands_run_in_priority_order(tmp_path):
    log = tmp_path / "order.log"
    for name, priority in (("first", 300), ("second", 200), ("third", 100)):
        write_script_agent(
            tmp_path, name, ORDER_SCRIPT.format(log=str(log), name=name), priority=priority
        )

    registry, phase = await build(tmp_path)
    try:
        results = await phase.run(message(), RecordingSink())
    finally:
        await registry.aclose()

    assert [item.agent for item in results] == ["first", "second", "third"]
    assert log.read_text().split() == ["first", "second", "third"]


CONCURRENT_SCRIPT = """\
import asyncio
async def run(message):
    await asyncio.sleep(0.3)
    return "done"
"""


async def test_agents_in_one_band_run_concurrently(tmp_path):
    for name in ("a", "b", "c"):
        write_script_agent(tmp_path, name, CONCURRENT_SCRIPT, priority=100)

    registry, phase = await build(tmp_path)
    try:
        started = asyncio.get_running_loop().time()
        results = await phase.run(message(), RecordingSink())
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        await registry.aclose()

    assert len(results) == 3
    # Serial would be ~0.9s; concurrent is ~0.3s.
    assert elapsed < 0.7, f"band took {elapsed:.2f}s, so it ran serially"


async def test_bands_are_sequential_not_concurrent(tmp_path):
    write_script_agent(tmp_path, "early", CONCURRENT_SCRIPT, priority=200)
    write_script_agent(tmp_path, "late", CONCURRENT_SCRIPT, priority=100)

    registry, phase = await build(tmp_path)
    try:
        started = asyncio.get_running_loop().time()
        await phase.run(message(), RecordingSink())
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        await registry.aclose()

    assert elapsed >= 0.6, f"bands took {elapsed:.2f}s, so they overlapped"


# --- accumulation --------------------------------------------------------------------


async def test_later_bands_see_earlier_output(tmp_path):
    write_script_agent(
        tmp_path, "producer", "def run(message):\n    return 'TICKET-1'\n", priority=200
    )
    write_script_agent(
        tmp_path,
        "consumer",
        "def run(message):\n"
        "    prior = {p['agent']: p['output'] for p in message['prior_outputs']}\n"
        "    return 'saw ' + prior.get('producer', 'nothing')\n",
        priority=100,
    )

    registry, phase = await build(tmp_path)
    try:
        results = await phase.run(message(), RecordingSink())
    finally:
        await registry.aclose()

    outputs = {item.agent: item.output for item in results}
    assert outputs["consumer"] == "saw TICKET-1"


async def test_peers_in_the_same_band_do_not_see_each_other(tmp_path):
    """They run concurrently, so peer output would be a race."""
    for name in ("a", "b"):
        write_script_agent(
            tmp_path,
            name,
            "def run(message):\n    return str(len(message['prior_outputs']))\n",
            priority=100,
        )

    registry, phase = await build(tmp_path)
    try:
        results = await phase.run(message(), RecordingSink())
    finally:
        await registry.aclose()

    assert {item.output for item in results} == {"0"}


async def test_payload_carries_the_message_and_agent_dir(tmp_path):
    write_script_agent(
        tmp_path,
        "probe",
        "import json\n"
        "def run(message):\n"
        "    return json.dumps(sorted(message.keys()))\n",
    )

    registry, phase = await build(tmp_path)
    try:
        results = await phase.run(
            message("hi", user="U1", channel="C1", thread="1.0"), RecordingSink()
        )
    finally:
        await registry.aclose()

    keys = results[0].output
    for expected in ("text", "user", "channel", "thread", "meta", "agent", "agent_dir",
                     "prior_outputs"):
        assert f'"{expected}"' in keys


# --- triggers ------------------------------------------------------------------------


async def test_only_matching_agents_run(tmp_path):
    write_script_agent(
        tmp_path, "matches", ECHO.format(name="matches"), trigger='text.contains("=====")'
    )
    write_script_agent(
        tmp_path, "skipped", ECHO.format(name="skipped"), trigger='text.contains("nope")'
    )
    write_script_agent(tmp_path, "always", ECHO.format(name="always"))

    registry, phase = await build(tmp_path)
    try:
        results = await phase.run(message("===== x ====="), RecordingSink())
    finally:
        await registry.aclose()

    assert sorted(item.agent for item in results) == ["always", "matches"]


async def test_channel_guard_excludes(tmp_path):
    write_script_agent(
        tmp_path,
        "ticket",
        ECHO.format(name="ticket"),
        trigger='text.contains("=====") and channel.notContains("PODUEMCJE")',
    )

    registry, phase = await build(tmp_path)
    try:
        allowed = await phase.run(message("===== x =====", channel="C0SUP"), RecordingSink())
        blocked = await phase.run(
            message("===== x =====", channel="PODUEMCJE-1"), RecordingSink()
        )
    finally:
        await registry.aclose()

    assert [item.agent for item in allowed] == ["ticket"]
    assert blocked == []


async def test_nothing_matches_means_no_results_and_no_events(tmp_path):
    write_script_agent(
        tmp_path, "ticket", ECHO.format(name="ticket"), trigger='text.contains("=====")'
    )

    registry, phase = await build(tmp_path)
    sink = RecordingSink()
    try:
        results = await phase.run(message("an ordinary question"), sink)
    finally:
        await registry.aclose()

    assert results == []
    assert sink.events == []
    assert sink.messages == []


# --- send_output ---------------------------------------------------------------------


async def test_send_output_true_posts_to_the_client(tmp_path):
    write_script_agent(
        tmp_path, "teller", "def run(message):\n    return 'Created TICKET-1'\n",
        send_output=True,
    )

    registry, phase = await build(tmp_path)
    sink = RecordingSink()
    try:
        results = await phase.run(message(), sink)
    finally:
        await registry.aclose()

    assert sink.messages == ["Created TICKET-1"]
    assert results[0].sent_to_client is True


async def test_send_output_false_keeps_it_internal(tmp_path):
    write_script_agent(
        tmp_path, "quiet", "def run(message):\n    return 'internal detail'\n"
    )

    registry, phase = await build(tmp_path)
    sink = RecordingSink()
    try:
        results = await phase.run(message(), sink)
    finally:
        await registry.aclose()

    assert sink.messages == []
    assert results[0].sent_to_client is False
    # Still available downstream.
    assert results[0].output == "internal detail"
    assert "internal context" in results[0].as_context()


async def test_send_output_with_empty_output_posts_nothing(tmp_path):
    write_script_agent(tmp_path, "empty", "def run(message):\n    return ''\n", send_output=True)

    registry, phase = await build(tmp_path)
    sink = RecordingSink()
    try:
        results = await phase.run(message(), sink)
    finally:
        await registry.aclose()

    assert sink.messages == []
    assert results[0].sent_to_client is False


def test_as_context_distinguishes_seen_from_unseen():
    from stark.types import ScriptResult

    seen = ScriptResult(agent="a", output="x", sent_to_client=True)
    unseen = ScriptResult(agent="b", output="y")
    failed = ScriptResult(agent="c", error="boom")

    assert "already shown to the user" in seen.as_context()
    assert "internal context" in unseen.as_context()
    assert "failed" in failed.as_context() and "boom" in failed.as_context()


# --- fail-open -----------------------------------------------------------------------


async def test_a_failing_script_does_not_stop_later_bands(tmp_path):
    write_script_agent(
        tmp_path, "broken", "def run(message):\n    raise RuntimeError('boom')\n", priority=200
    )
    write_script_agent(tmp_path, "survivor", ECHO.format(name="survivor"), priority=100)

    registry, phase = await build(tmp_path)
    sink = RecordingSink()
    try:
        results = await phase.run(message(), sink)
    finally:
        await registry.aclose()

    outcomes = {item.agent: item for item in results}
    assert outcomes["broken"].succeeded is False
    assert outcomes["survivor"].output == "survivor"
    assert any(kind == "agent_error" for kind, _, _ in sink.events)


async def test_a_failing_peer_does_not_stop_its_band(tmp_path):
    write_script_agent(
        tmp_path, "broken", "def run(message):\n    raise RuntimeError('boom')\n", priority=100
    )
    write_script_agent(tmp_path, "healthy", ECHO.format(name="healthy"), priority=100)

    registry, phase = await build(tmp_path)
    try:
        results = await phase.run(message(), RecordingSink())
    finally:
        await registry.aclose()

    assert {item.agent for item in results} == {"broken", "healthy"}


async def test_a_script_that_fails_to_load_is_reported_at_run_time(tmp_path, caplog):
    write_script_agent(tmp_path, "unloadable", "def run(:\n")

    with caplog.at_level("ERROR"):
        registry, phase = await build(tmp_path)
    sink = RecordingSink()
    try:
        results = await phase.run(message(), sink)
    finally:
        await registry.aclose()

    assert "will not run" in caplog.text
    assert results[0].succeeded is False
    assert "not loaded" in (results[0].error or "")


# --- progress events -----------------------------------------------------------------


async def test_each_script_agent_emits_a_start_and_end_keyed_by_name(tmp_path):
    write_script_agent(tmp_path, "one", ECHO.format(name="one"))

    registry, phase = await build(tmp_path)
    sink = RecordingSink()
    try:
        await phase.run(message(), sink)
    finally:
        await registry.aclose()

    kinds = [(kind, key) for kind, _, key in sink.events]
    assert ("agent_start", "one") in kinds
    assert ("agent_end", "one") in kinds


# --- interaction with llm agents -----------------------------------------------------


async def test_script_agents_are_offered_to_the_orchestrator_by_default(tmp_path):
    write_script_agent(tmp_path, "deterministic", ECHO.format(name="deterministic"))
    write_llm_agent(tmp_path, "reasoner")

    registry = await Registry.create(tmp_path)
    try:
        names = {tool["function"]["name"] for tool in registry.delegation_tools()}
        assert names == {"agent__reasoner", "agent__deterministic"}
        assert registry.has_llm_agents is True
        assert [agent.name for agent in registry.script_agents] == ["deterministic"]
    finally:
        await registry.aclose()


async def test_registry_with_only_script_agents_has_no_llm_agents(tmp_path):
    write_script_agent(tmp_path, "deterministic", ECHO.format(name="deterministic"))

    registry = await Registry.create(tmp_path)
    try:
        assert registry.has_llm_agents is False
        # The tool exists, but with no llm agents the orchestrator never runs to use it.
        assert [tool["function"]["name"] for tool in registry.delegation_tools()] == [
            "agent__deterministic"
        ]
    finally:
        await registry.aclose()


async def test_warning_when_output_has_nowhere_to_go(tmp_path, caplog):
    """No llm agents and send_output off means the string is discarded."""
    write_script_agent(tmp_path, "stranded", ECHO.format(name="stranded"))

    with caplog.at_level("WARNING"):
        registry = await Registry.create(tmp_path)
    await registry.aclose()

    assert "output goes" in caplog.text
    assert "stranded" in caplog.text


async def test_no_warning_when_an_llm_agent_can_receive_the_output(tmp_path, caplog):
    write_script_agent(tmp_path, "quiet", ECHO.format(name="quiet"))
    write_llm_agent(tmp_path, "reasoner")

    with caplog.at_level("WARNING"):
        registry = await Registry.create(tmp_path)
    await registry.aclose()

    assert "output goes" not in caplog.text


async def test_roster_lists_script_agents_separately(tmp_path):
    write_script_agent(
        tmp_path, "deterministic", ECHO.format(name="deterministic"),
        priority=200, send_output=True, trigger='text.contains("=====")',
    )
    write_llm_agent(tmp_path, "reasoner")

    registry = await Registry.create(tmp_path)
    try:
        roster = registry.roster()
    finally:
        await registry.aclose()

    assert "agent__reasoner" in roster
    assert "Script agents" in roster
    assert "priority 200" in roster
    assert "send_output" in roster
    assert "before_orchestrator" in roster
    # Delegatable by default, so it is advertised with a tool name and marked as a script.
    assert "agent__deterministic" in roster
    assert "deterministic script" in roster


async def test_roster_marks_a_hidden_script_agent_as_not_delegatable(tmp_path):
    write_script_agent(
        tmp_path, "hidden", ECHO.format(name="hidden"), avoid_orchestrator=True
    )
    write_llm_agent(tmp_path, "reasoner")

    registry = await Registry.create(tmp_path)
    try:
        roster = registry.roster()
    finally:
        await registry.aclose()

    assert "not delegatable" in roster
    assert "agent__hidden" not in roster
