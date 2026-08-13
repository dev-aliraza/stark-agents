"""`stop_execution`: a script agent halting the run, and the UX still closing out cleanly.

The contract has two halves. Nothing downstream runs — later bands, the orchestrator, the
after phase. But everything that *did* run is still settled: steps struck through, output
already posted left standing, no dangling progress.
"""

from __future__ import annotations

import json

import pytest

import stark
from stark.listeners.base import Message, ResponseSink
from stark.llm import client as llm_client
from stark.orchestration import (
    Orchestrator,
    Registry,
    ScriptPhase,
    ScriptRunner,
    build_payload,
    load_entry_point,
    stop_requested,
)
from stark.orchestration.script_runner import extract_stop
from stark.parsers import discover_agents
from stark.types import TRIGGER_POINT_AFTER, Completion, ModelConfig, ScriptResult, ToolCall

STOP = "def run(message):\n    return {'stop_execution': True}\n"
STOP_WITH_OUTPUT = (
    "def run(message):\n"
    "    return {'stop_execution': True, 'output': 'Ignored: duplicate report'}\n"
)


def write_script_agent(root, name: str, body: str, **metadata) -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "handler.py").write_text(body, encoding="utf-8")

    extra = ""
    for key, value in metadata.items():
        if isinstance(value, bool):
            value = str(value).lower()
        elif isinstance(value, str):
            value = f"'{value}'"
        extra += f"{key}: {value}\n"

    (directory / "AGENT.md").write_text(
        f"---\nname: {name}\ndescription: {name} does one thing.\n"
        f"type: script\nscript: handler.py\n{extra}---\n\nBody.\n",
        encoding="utf-8",
    )


def write_llm_agent(root, name: str = "reasoner") -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "AGENT.md").write_text(
        f"---\nname: {name}\ndescription: Reasons about things.\n"
        "provider: anthropic\nmodel: claude-opus-5\n---\n\nBody.\n",
        encoding="utf-8",
    )


class RecordingSink(ResponseSink):
    def __init__(self):
        self.messages: list[str] = []
        self.events: list[tuple[str, str, str | None]] = []
        self.final_text: str | None = None
        self.finals = 0
        self.settled = 0

    async def chunk(self, text: str) -> None:
        pass

    async def message(self, text: str) -> None:
        self.messages.append(text)

    async def event(self, kind: str, detail: str, key: str | None = None) -> None:
        self.events.append((kind, detail, key))

    async def final(self, text: str) -> None:
        self.final_text = text
        self.finals += 1

    async def error(self, text: str) -> None:
        pass

    async def settle(self) -> None:
        self.settled += 1


async def phase(tmp_path, trigger_point: str = "before_orchestrator"):
    registry = await Registry.create(tmp_path)
    agents = (
        registry.script_agents_before
        if trigger_point == "before_orchestrator"
        else registry.script_agents_after
    )
    return registry, ScriptPhase(agents, registry.script_runners(), trigger_point)


# --- reading the flag off the return value -------------------------------------------


def test_a_plain_string_return_never_stops():
    assert extract_stop("all good", "a") == (False, "all good")


def test_a_dict_without_the_key_is_untouched():
    assert extract_stop({"ticket": "X"}, "a") == (False, {"ticket": "X"})


def test_the_flag_is_stripped_from_the_output():
    """It is a control signal, so it must not leak into what the user reads."""
    assert extract_stop({"stop_execution": True}, "a") == (True, None)


def test_an_output_key_becomes_the_output():
    assert extract_stop({"stop_execution": True, "output": "blocked"}, "a") == (
        True,
        "blocked",
    )


def test_other_keys_survive_as_the_output():
    stop, output = extract_stop({"stop_execution": True, "reason": "spam"}, "a")
    assert (stop, output) == (True, {"reason": "spam"})


def test_stop_false_is_honoured():
    assert extract_stop({"stop_execution": False, "output": "carry on"}, "a") == (
        False,
        "carry on",
    )


@pytest.mark.parametrize("raw", [True, "true", "yes", "on", 1])
def test_truthy_spellings(raw):
    assert extract_stop({"stop_execution": raw}, "a")[0] is True


@pytest.mark.parametrize("raw", [False, "false", "no", "off", 0, ""])
def test_falsy_spellings(raw):
    assert extract_stop({"stop_execution": raw}, "a")[0] is False


def test_a_nonsense_flag_is_false_and_warned_about(caplog):
    with caplog.at_level("WARNING"):
        stop, _ = extract_stop({"stop_execution": "maybe"}, "gatekeeper")

    assert stop is False
    assert "not a boolean" in caplog.text
    assert "gatekeeper" in caplog.text


async def test_the_runner_records_the_flag_and_the_output(tmp_path):
    write_script_agent(tmp_path, "gate", STOP_WITH_OUTPUT)
    agent = discover_agents(tmp_path)[0]
    runner = ScriptRunner(agent, load_entry_point(agent))

    result = await runner.run(build_payload(agent, Message(text="hi")))

    assert result.stop_execution is True
    assert result.output == "Ignored: duplicate report"
    assert result.succeeded


async def test_a_failing_script_cannot_stop_the_run(tmp_path):
    """Fail-open is the older promise, and a crash is not a decision to halt."""
    write_script_agent(tmp_path, "broken", "def run(m):\n    raise RuntimeError('boom')\n")
    agent = discover_agents(tmp_path)[0]
    runner = ScriptRunner(agent, load_entry_point(agent))

    result = await runner.run(build_payload(agent, Message(text="hi")))

    assert result.succeeded is False
    assert result.stop_execution is False


def test_stop_requested_finds_the_first_halting_result():
    results = [
        ScriptResult(agent="a"),
        ScriptResult(agent="b", stop_execution=True),
        ScriptResult(agent="c", stop_execution=True),
    ]
    assert stop_requested(results).agent == "b"
    assert stop_requested([ScriptResult(agent="a")]) is None
    assert stop_requested([]) is None


# --- stopping a phase ----------------------------------------------------------------


BEFORE = {"triggerPoint": "before_orchestrator"}


async def test_later_bands_do_not_run(tmp_path):
    write_script_agent(tmp_path, "gate", STOP, priority=300, **BEFORE)
    write_script_agent(
        tmp_path, "never", "def run(m):\n    return 'RAN'\n", priority=100, **BEFORE
    )

    registry, script_phase = await phase(tmp_path)
    sink = RecordingSink()
    try:
        results = await script_phase.run(Message(text="hi"), sink)
    finally:
        await registry.aclose()

    assert [item.agent for item in results] == ["gate"]
    # Not even a progress step for the agent that never ran.
    assert all("never" not in detail for _, detail, _ in sink.events)


async def test_the_stopping_agent_still_reports_as_finished(tmp_path):
    """The UX half of the contract: nothing is left spinning."""
    write_script_agent(tmp_path, "gate", STOP, priority=300, **BEFORE)
    write_script_agent(
        tmp_path, "never", "def run(m):\n    return 'RAN'\n", priority=100, **BEFORE
    )

    registry, script_phase = await phase(tmp_path)
    sink = RecordingSink()
    try:
        await script_phase.run(Message(text="hi"), sink)
    finally:
        await registry.aclose()

    assert ("agent_start", "gate (script)", "gate") in sink.events
    assert ("agent_end", "gate (script)", "gate") in sink.events


async def test_output_is_still_posted_when_stopping(tmp_path):
    write_script_agent(
        tmp_path, "gate", STOP_WITH_OUTPUT, send_output=True, priority=300, **BEFORE
    )
    write_script_agent(
        tmp_path, "never", "def run(m):\n    return 'RAN'\n", priority=100, **BEFORE
    )

    registry, script_phase = await phase(tmp_path)
    sink = RecordingSink()
    try:
        results = await script_phase.run(Message(text="hi"), sink)
    finally:
        await registry.aclose()

    assert sink.messages == ["Ignored: duplicate report"]
    assert results[0].sent_to_client is True


async def test_peers_in_the_same_band_still_complete(tmp_path):
    """They were started together, so the flag cannot un-run them."""
    write_script_agent(tmp_path, "gate", STOP, priority=100, **BEFORE)
    write_script_agent(
        tmp_path, "peer", "def run(m):\n    return 'RAN'\n", priority=100, **BEFORE
    )

    registry, script_phase = await phase(tmp_path)
    try:
        results = await script_phase.run(Message(text="hi"), RecordingSink())
    finally:
        await registry.aclose()

    assert {item.agent for item in results} == {"gate", "peer"}
    assert stop_requested(results).agent == "gate"


async def test_a_stop_in_the_after_phase_skips_later_after_bands(tmp_path):
    write_script_agent(
        tmp_path, "gate", STOP, triggerPoint="after_orchestrator", priority=300
    )
    write_script_agent(
        tmp_path, "never", "def run(m):\n    return 'RAN'\n",
        triggerPoint="after_orchestrator", priority=100,
    )

    registry, script_phase = await phase(tmp_path, TRIGGER_POINT_AFTER)
    try:
        results = await script_phase.run(Message(text="hi"), RecordingSink())
    finally:
        await registry.aclose()

    assert [item.agent for item in results] == ["gate"]


async def test_the_skipped_agents_are_logged(tmp_path, caplog):
    write_script_agent(tmp_path, "gate", STOP, priority=300, **BEFORE)
    write_script_agent(
        tmp_path, "never", "def run(m):\n    return 'RAN'\n", priority=100, **BEFORE
    )

    registry, script_phase = await phase(tmp_path)
    try:
        with caplog.at_level("INFO"):
            await script_phase.run(Message(text="hi"), RecordingSink())
    finally:
        await registry.aclose()

    assert "stopped the before_orchestrator phase" in caplog.text
    assert "skipping never" in caplog.text


# --- end to end through the runtime --------------------------------------------------


@pytest.fixture()
def model(monkeypatch):
    class Recorder:
        def __init__(self):
            self.calls: list[dict] = []

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            return Completion(content="ORCHESTRATOR ANSWER")

    recorder = Recorder()
    monkeypatch.setattr(llm_client.LLMClient, "complete", staticmethod(recorder.complete))
    return recorder


def feed(monkeypatch, *lines: str) -> None:
    queue = iter(lines)
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: next(queue))


async def test_the_orchestrator_is_never_called(tmp_path, monkeypatch, capsys, model):
    write_script_agent(
        tmp_path, "gate", STOP_WITH_OUTPUT, triggerPoint="before_orchestrator",
        send_output=True, avoid_orchestrator=True,
    )
    write_llm_agent(tmp_path)
    feed(monkeypatch, "hello", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")
    out = capsys.readouterr().out

    assert model.calls == []
    # What the script said stands as the whole reply.
    assert "Ignored: duplicate report" in out
    assert "ORCHESTRATOR ANSWER" not in out


async def test_the_after_phase_is_skipped_too(tmp_path, monkeypatch, capsys, model):
    write_script_agent(
        tmp_path, "gate", STOP, triggerPoint="before_orchestrator", avoid_orchestrator=True
    )
    write_script_agent(
        tmp_path, "archiver", "def run(m):\n    return 'ARCHIVED'\n",
        triggerPoint="after_orchestrator", send_output=True, avoid_orchestrator=True,
    )
    write_llm_agent(tmp_path)
    feed(monkeypatch, "hello", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")
    out = capsys.readouterr().out

    assert model.calls == []
    assert "ARCHIVED" not in out


async def test_a_stop_in_the_after_phase_leaves_the_answer_standing(
    tmp_path, monkeypatch, capsys, model
):
    """Stopping after the answer is out cannot retract it — only skip what follows."""
    write_script_agent(
        tmp_path, "gate", STOP, triggerPoint="after_orchestrator",
        priority=300, avoid_orchestrator=True,
    )
    write_script_agent(
        tmp_path, "never", "def run(m):\n    return 'LATER'\n",
        triggerPoint="after_orchestrator", priority=100, send_output=True,
        avoid_orchestrator=True,
    )
    write_llm_agent(tmp_path)
    feed(monkeypatch, "hello", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")
    out = capsys.readouterr().out

    assert "ORCHESTRATOR ANSWER" in out
    assert "LATER" not in out


async def test_the_run_result_names_who_stopped_it(tmp_path):
    """Checked through the real handler, via a sink that captures the result."""
    write_script_agent(
        tmp_path, "gate", STOP, triggerPoint="before_orchestrator", avoid_orchestrator=True
    )

    registry, script_phase = await phase(tmp_path)
    try:
        results = await script_phase.run(Message(text="hi"), RecordingSink())
    finally:
        await registry.aclose()

    halt = stop_requested(results)
    assert halt is not None and halt.agent == "gate"


async def test_the_cli_footer_says_who_stopped_the_run(tmp_path, monkeypatch, capsys, model):
    """A halted run must not read as one that simply had nothing to say."""
    write_script_agent(
        tmp_path, "gate", STOP, triggerPoint="before_orchestrator", avoid_orchestrator=True
    )
    write_llm_agent(tmp_path)
    feed(monkeypatch, "hello", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")

    assert "stopped by gate" in capsys.readouterr().out


async def test_the_response_is_closed_out_exactly_once(tmp_path, monkeypatch, model):
    """No dangling progress, and no second `final` either."""
    from stark.listeners.cli import CLISink

    finals: list[str] = []
    original = CLISink.final

    async def counting_final(self, text: str) -> None:
        finals.append(text)
        await original(self, text)

    monkeypatch.setattr(CLISink, "final", counting_final)
    write_script_agent(
        tmp_path, "gate", STOP, triggerPoint="before_orchestrator", avoid_orchestrator=True
    )
    write_llm_agent(tmp_path)
    feed(monkeypatch, "hello", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")

    assert finals == [""]


async def test_slack_strikes_every_step_when_a_script_stops(tmp_path):
    pytest.importorskip("slack_bolt", reason="the slack listener needs the [slack] extra")
    from stark.config import SlackConfig
    from stark.listeners.slack import SlackSink

    class FakeClient:
        def __init__(self):
            self.posted: list[dict] = []
            self.updates: list[dict] = []

        async def chat_postMessage(self, **kwargs):
            self.posted.append(kwargs)
            return {"ts": f"{len(self.posted)}.0"}

        async def chat_update(self, **kwargs):
            self.updates.append(kwargs)
            return {"ok": True}

    write_script_agent(
        tmp_path, "gate", STOP_WITH_OUTPUT, triggerPoint="before_orchestrator",
        send_output=True, avoid_orchestrator=True,
    )

    client = FakeClient()
    sink = SlackSink(
        client, channel="C1", thread_ts="T1", config=SlackConfig(update_interval=0)
    )
    await sink.status("working")

    registry, script_phase = await phase(tmp_path)
    try:
        await script_phase.run(Message(text="hi"), sink)
    finally:
        await registry.aclose()
    # What runtime.handle does when a script halts the run.
    await sink.final("")

    progress = client.updates[-1]["text"]
    assert progress == ":white_check_mark: ~gate (script)~"
    assert ":hourglass:" not in progress
    # The script's own output was posted; no empty answer message followed it.
    assert [item["text"] for item in client.posted[1:]] == ["Ignored: duplicate report"]


# --- a delegated script agent stopping the run ---------------------------------------


def tool_call(name: str, task: str, index: int = 0) -> ToolCall:
    return ToolCall(id=f"call_{index}", name=name, arguments=json.dumps({"task": task}))


async def test_a_delegated_script_can_stop_the_orchestrator(tmp_path, monkeypatch):
    calls: list[dict] = []

    async def complete(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return Completion(tool_calls=[tool_call("agent__gate", "check it")])
        return Completion(content="SHOULD NOT BE REACHED")

    monkeypatch.setattr(llm_client.LLMClient, "complete", staticmethod(complete))
    write_script_agent(tmp_path, "gate", STOP_WITH_OUTPUT, send_output=True)
    write_llm_agent(tmp_path)

    registry = await Registry.create(tmp_path)
    sink = RecordingSink()
    try:
        result = await Orchestrator(registry, "Be helpful.", ModelConfig()).handle(
            Message(text="hi"), sink
        )
    finally:
        await registry.aclose()

    # The model is not consulted again, so it never gets to answer around the halt.
    assert len(calls) == 1
    assert result.stopped is True
    assert result.stopped_by == "gate"
    assert result.output == ""
    assert sink.final_text == ""
    # The script's own output still reached the user, and its step closed.
    assert sink.messages == ["Ignored: duplicate report"]
    assert ("agent_end", "gate finished", "call_0") in sink.events


async def test_a_delegated_script_that_does_not_stop_lets_the_loop_continue(
    tmp_path, monkeypatch
):
    calls: list[dict] = []

    async def complete(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return Completion(tool_calls=[tool_call("agent__opener", "open it")])
        return Completion(content="Opened it.")

    monkeypatch.setattr(llm_client.LLMClient, "complete", staticmethod(complete))
    write_script_agent(tmp_path, "opener", "def run(m):\n    return 'SUPPORT-42'\n")
    write_llm_agent(tmp_path)

    registry = await Registry.create(tmp_path)
    try:
        result = await Orchestrator(registry, "Be helpful.", ModelConfig()).handle(
            Message(text="hi"), RecordingSink()
        )
    finally:
        await registry.aclose()

    assert result.stopped is False
    assert result.output == "Opened it."


async def test_a_stopping_script_result_handed_in_does_not_halt_the_loop(
    tmp_path, monkeypatch
):
    """Only this turn's delegations can stop it; the caller acts on what it passed in."""
    calls: list[dict] = []

    async def complete(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return Completion(tool_calls=[tool_call("agent__reasoner", "think")])
        return Completion(content="answered anyway")

    monkeypatch.setattr(llm_client.LLMClient, "complete", staticmethod(complete))
    write_llm_agent(tmp_path)

    registry = await Registry.create(tmp_path)
    try:
        result = await Orchestrator(registry, "Be helpful.", ModelConfig()).handle(
            Message(text="hi"),
            RecordingSink(),
            [ScriptResult(agent="earlier", stop_execution=True)],
        )
    finally:
        await registry.aclose()

    assert result.output == "answered anyway"
    assert result.stopped is False


async def test_the_after_phase_is_skipped_when_a_delegated_script_stops(
    tmp_path, monkeypatch, capsys
):
    calls: list[dict] = []

    async def complete(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return Completion(tool_calls=[tool_call("agent__gate", "check it")])
        return Completion(content="ORCHESTRATOR ANSWER")

    monkeypatch.setattr(llm_client.LLMClient, "complete", staticmethod(complete))
    write_script_agent(tmp_path, "gate", STOP)
    write_script_agent(
        tmp_path, "archiver", "def run(m):\n    return 'ARCHIVED'\n",
        triggerPoint="after_orchestrator", send_output=True, avoid_orchestrator=True,
    )
    write_llm_agent(tmp_path)
    feed(monkeypatch, "hello", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")
    out = capsys.readouterr().out

    assert "ARCHIVED" not in out
    assert "ORCHESTRATOR ANSWER" not in out
