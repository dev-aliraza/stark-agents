"""`avoid_orchestrator` and `triggerPoint`: how a script agent is reached, and when.

Three surfaces meet here — the parser that reads the two keys, the registry that decides
what the orchestrator is offered, and the runtime that runs a phase on each side of it.
"""

from __future__ import annotations

import json

import pytest

import stark
from stark.errors import AgentValidationError
from stark.listeners.base import Message, ResponseSink
from stark.llm import client as llm_client
from stark.orchestration import Orchestrator, Registry, ScriptPhase
from stark.parsers import discover_agents
from stark.types import (
    INVOCATION_DELEGATION,
    INVOCATION_TRIGGER,
    TRIGGER_POINT_AFTER,
    TRIGGER_POINT_BEFORE,
    Completion,
    ModelConfig,
    ToolCall,
)

ECHO = "def run(message):\n    return 'ran ' + message['agent']\n"


def write_script_agent(root, name: str, body: str = ECHO, **metadata) -> None:
    """Write a script agent, rendering any extra frontmatter key verbatim."""
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


def only(root, name: str = "one"):
    return {agent.name: agent for agent in discover_agents(root)}[name]


class RecordingSink(ResponseSink):
    def __init__(self):
        self.chunks: list[str] = []
        self.messages: list[str] = []
        self.events: list[tuple[str, str, str | None]] = []
        self.final_text: str | None = None
        self.settled = 0

    async def chunk(self, text: str) -> None:
        self.chunks.append(text)

    async def message(self, text: str) -> None:
        self.messages.append(text)

    async def event(self, kind: str, detail: str, key: str | None = None) -> None:
        self.events.append((kind, detail, key))

    async def final(self, text: str) -> None:
        self.final_text = text

    async def error(self, text: str) -> None:
        pass

    async def settle(self) -> None:
        self.settled += 1


# --- parsing --------------------------------------------------------------------------


def test_with_no_trigger_point_the_agent_only_runs_when_delegated_to(tmp_path):
    """The default is the quiet one: loaded as a tool, never firing on its own."""
    write_script_agent(tmp_path, "one")
    agent = only(tmp_path)

    assert agent.trigger_point is None
    assert agent.runs_automatically is False
    assert agent.runs_before_orchestrator is False
    assert agent.runs_after_orchestrator is False
    # Still reachable, because the orchestrator is offered it.
    assert agent.avoid_orchestrator is False
    assert agent.delegatable is True
    assert agent.reachable is True


def test_trigger_point_before_orchestrator(tmp_path):
    write_script_agent(tmp_path, "one", triggerPoint="before_orchestrator")
    agent = only(tmp_path)

    assert agent.trigger_point == TRIGGER_POINT_BEFORE
    assert agent.runs_automatically is True
    assert agent.runs_before_orchestrator is True
    assert agent.runs_after_orchestrator is False


def test_trigger_point_after_orchestrator(tmp_path):
    write_script_agent(tmp_path, "one", triggerPoint="after_orchestrator")
    agent = only(tmp_path)

    assert agent.trigger_point == TRIGGER_POINT_AFTER
    assert agent.runs_automatically is True
    assert agent.runs_after_orchestrator is True
    assert agent.runs_before_orchestrator is False


def test_a_trigger_rule_alone_does_not_make_an_agent_run(tmp_path, caplog):
    """The rule gates the automatic run; the trigger point is what creates one."""
    write_script_agent(tmp_path, "one", triggerRule='text.contains("=====")')

    with caplog.at_level("WARNING"):
        agent = only(tmp_path)

    assert agent.trigger_rule is not None
    assert agent.runs_automatically is False
    assert "does nothing without a 'triggerPoint'" in caplog.text


def test_no_warning_when_a_trigger_rule_has_a_trigger_point(tmp_path, caplog):
    write_script_agent(
        tmp_path, "one", triggerRule='text.contains("=====")',
        triggerPoint="before_orchestrator",
    )

    with caplog.at_level("WARNING"):
        only(tmp_path)

    assert "does nothing without" not in caplog.text


def test_trigger_point_is_case_insensitive(tmp_path):
    write_script_agent(tmp_path, "one", triggerPoint="After_Orchestrator")
    assert only(tmp_path).trigger_point == TRIGGER_POINT_AFTER


def test_an_unknown_trigger_point_is_rejected(tmp_path):
    """Defaulting a typo would silently move the agent to the wrong side of the model."""
    write_script_agent(tmp_path, "one", triggerPoint="during_orchestrator")

    with pytest.raises(AgentValidationError, match="unknown triggerPoint"):
        from stark.parsers import parse_agent_file

        parse_agent_file(tmp_path / "one" / "AGENT.md")


def test_an_unknown_trigger_point_skips_the_agent_without_stopping_discovery(tmp_path, caplog):
    write_script_agent(tmp_path, "bad", triggerPoint="whenever")
    write_script_agent(tmp_path, "good")

    with caplog.at_level("WARNING"):
        names = {agent.name for agent in discover_agents(tmp_path)}

    assert names == {"good"}
    assert "unknown triggerPoint" in caplog.text


def test_avoid_orchestrator_hides_the_agent(tmp_path):
    write_script_agent(
        tmp_path, "one", avoid_orchestrator=True, triggerPoint="before_orchestrator"
    )
    agent = only(tmp_path)

    assert agent.avoid_orchestrator is True
    assert agent.delegatable is False
    # It still runs on its own; only the tool exposure is withdrawn.
    assert agent.runs_before_orchestrator is True
    assert agent.reachable is True


def test_avoid_orchestrator_false_is_the_same_as_omitting_it(tmp_path):
    write_script_agent(tmp_path, "one", avoid_orchestrator=False)
    assert only(tmp_path).delegatable is True


def test_hiding_an_agent_that_has_no_trigger_point_is_flagged(tmp_path, caplog):
    """Neither way in is open, so it is dead configuration."""
    write_script_agent(tmp_path, "one", avoid_orchestrator=True)

    with caplog.at_level("WARNING"):
        agent = only(tmp_path)

    assert agent.reachable is False
    assert "can never run" in caplog.text


def test_an_llm_agent_is_warned_that_the_new_keys_do_nothing(tmp_path, caplog):
    directory = tmp_path / "reasoner"
    directory.mkdir()
    (directory / "AGENT.md").write_text(
        "---\nname: reasoner\ndescription: Reasons.\nprovider: anthropic\n"
        "model: claude-opus-5\ntriggerPoint: after_orchestrator\n"
        "avoid_orchestrator: true\n---\n\nBody.\n",
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        agent = only(tmp_path, "reasoner")

    assert "'triggerPoint'" in caplog.text
    assert "'avoid_orchestrator'" in caplog.text
    # Ignored, so an llm agent stays delegatable — it has no other way in.
    assert agent.delegatable is True


# --- what the registry offers ---------------------------------------------------------


async def test_the_registry_splits_the_two_phases(tmp_path):
    write_script_agent(tmp_path, "early", triggerPoint="before_orchestrator", priority=300)
    write_script_agent(tmp_path, "late", triggerPoint="after_orchestrator", priority=50)
    write_script_agent(tmp_path, "also-late", triggerPoint="after_orchestrator", priority=400)
    write_script_agent(tmp_path, "on-request", priority=999)

    registry = await Registry.create(tmp_path)
    try:
        assert [agent.name for agent in registry.script_agents_before] == ["early"]
        # Highest priority first, independently of the other phase.
        assert [agent.name for agent in registry.script_agents_after] == ["also-late", "late"]
        # No triggerPoint, so it belongs to neither phase whatever its priority.
        assert "on-request" in {agent.name for agent in registry.script_agents}
    finally:
        await registry.aclose()


async def test_an_agent_with_no_trigger_point_never_fires_on_a_message(tmp_path):
    write_script_agent(tmp_path, "on-request")

    registry = await Registry.create(tmp_path)
    try:
        before = ScriptPhase(registry.script_agents_before, registry.script_runners())
        after = ScriptPhase(
            registry.script_agents_after, registry.script_runners(), TRIGGER_POINT_AFTER
        )
        sink = RecordingSink()
        assert await before.run(Message(text="hi"), sink) == []
        assert await after.run(Message(text="hi"), sink) == []
    finally:
        await registry.aclose()

    # Not even a progress step: as far as this message went, the agent does not exist.
    assert sink.events == []


async def test_a_hidden_script_agent_gets_no_delegation_tool(tmp_path):
    write_script_agent(tmp_path, "visible")
    write_script_agent(tmp_path, "hidden", avoid_orchestrator=True)
    write_llm_agent(tmp_path)

    registry = await Registry.create(tmp_path)
    try:
        names = {tool["function"]["name"] for tool in registry.delegation_tools()}
        assert names == {"agent__reasoner", "agent__visible"}
        assert registry.is_agent_tool("agent__hidden") is False
        # It is still loaded and still runs in its phase.
        assert registry.script_runner_for(only(tmp_path, "hidden")) is not None
    finally:
        await registry.aclose()


async def test_a_delegated_script_tool_is_described_as_deterministic(tmp_path):
    write_script_agent(tmp_path, "visible")
    write_llm_agent(tmp_path)

    registry = await Registry.create(tmp_path)
    try:
        tool = next(
            item
            for item in registry.delegation_tools()
            if item["function"]["name"] == "agent__visible"
        )
    finally:
        await registry.aclose()

    description = tool["function"]["description"]
    assert "visible does one thing." in description
    assert "fixed script, not a model" in description
    # The schema matches an llm agent's, so the model has one calling convention.
    assert tool["function"]["parameters"]["required"] == ["task"]


async def test_a_hidden_script_agent_still_runs_in_its_phase(tmp_path):
    write_script_agent(
        tmp_path, "hidden", avoid_orchestrator=True, triggerPoint="before_orchestrator"
    )

    registry = await Registry.create(tmp_path)
    try:
        phase = ScriptPhase(registry.script_agents_before, registry.script_runners())
        results = await phase.run(Message(text="hi"), RecordingSink())
    finally:
        await registry.aclose()

    assert [item.agent for item in results] == ["hidden"]


async def test_warning_when_an_unconditional_script_is_also_delegatable(tmp_path, caplog):
    """It runs on every message and the model can call it again for the same one."""
    write_script_agent(tmp_path, "twice", triggerPoint="before_orchestrator")
    write_llm_agent(tmp_path)

    with caplog.at_level("WARNING"):
        registry = await Registry.create(tmp_path)
    await registry.aclose()

    assert "may call them a second time" in caplog.text
    assert "twice" in caplog.text


async def test_no_double_run_warning_when_a_trigger_rule_gates_it(tmp_path, caplog):
    write_script_agent(
        tmp_path, "gated", triggerRule='text.contains("=====")',
        triggerPoint="before_orchestrator",
    )
    write_llm_agent(tmp_path)

    with caplog.at_level("WARNING"):
        registry = await Registry.create(tmp_path)
    await registry.aclose()

    assert "may call them a second time" not in caplog.text


async def test_no_double_run_warning_when_the_agent_is_hidden(tmp_path, caplog):
    write_script_agent(
        tmp_path, "quiet", avoid_orchestrator=True, triggerPoint="before_orchestrator"
    )
    write_llm_agent(tmp_path)

    with caplog.at_level("WARNING"):
        registry = await Registry.create(tmp_path)
    await registry.aclose()

    assert "may call them a second time" not in caplog.text


async def test_no_double_run_warning_without_a_trigger_point(tmp_path, caplog):
    """Nothing to duplicate: delegation is the only way in."""
    write_script_agent(tmp_path, "on-request")
    write_llm_agent(tmp_path)

    with caplog.at_level("WARNING"):
        registry = await Registry.create(tmp_path)
    await registry.aclose()

    assert "may call them a second time" not in caplog.text


async def test_warning_when_nothing_can_ever_delegate(tmp_path, caplog):
    """Delegation is its only way in, and with no llm agents nothing ever delegates."""
    write_script_agent(tmp_path, "orphan", send_output=True)

    with caplog.at_level("WARNING"):
        registry = await Registry.create(tmp_path)
    await registry.aclose()

    assert "nothing will ever delegate" in caplog.text
    assert "orphan" in caplog.text


async def test_no_orphan_warning_when_a_trigger_point_gives_it_a_way_in(tmp_path, caplog):
    write_script_agent(
        tmp_path, "standalone", send_output=True, triggerPoint="before_orchestrator"
    )

    with caplog.at_level("WARNING"):
        registry = await Registry.create(tmp_path)
    await registry.aclose()

    assert "nothing will ever delegate" not in caplog.text


async def test_two_agents_slugging_to_one_tool_name_are_reported(tmp_path, caplog):
    """Distinct names, one tool name — the orchestrator could only ever reach one."""
    for directory, name in (("first", "build report"), ("second", "build+report")):
        write_script_agent(tmp_path, directory)
        (tmp_path / directory / "AGENT.md").write_text(
            f"---\nname: {name}\ndescription: Builds a report.\n"
            "type: script\nscript: handler.py\n---\n\nBody.\n",
            encoding="utf-8",
        )

    with caplog.at_level("WARNING"):
        registry = await Registry.create(tmp_path)
    try:
        assert len(registry.delegation_tools()) == 1
    finally:
        await registry.aclose()

    assert "both map to the tool name" in caplog.text


# --- delegation to a script agent -----------------------------------------------------


def stub(monkeypatch, *completions: Completion) -> list[dict]:
    """Replay completions in order and hand back the recorded requests."""
    calls: list[dict] = []
    script = list(completions)

    async def complete(**kwargs):
        calls.append(kwargs)
        return script.pop(0) if script else Completion(content="done")

    monkeypatch.setattr(llm_client.LLMClient, "complete", staticmethod(complete))
    return calls


def tool_call(name: str, task: str, context: str = "", index: int = 0) -> ToolCall:
    arguments = {"task": task}
    if context:
        arguments["context"] = context
    return ToolCall(id=f"call_{index}", name=name, arguments=json.dumps(arguments))


def tool_messages(calls: list[dict]) -> list[dict]:
    return [msg for msg in calls[-1]["messages"] if msg["role"] == "tool"]


PROBE = """\
import json
def run(message):
    return json.dumps(
        {
            "invocation": message["invocation"],
            "task": message["task"],
            "context": message["context"],
            "text": message["text"],
        }
    )
"""


async def test_the_orchestrator_can_call_a_script_agent(tmp_path, monkeypatch):
    write_script_agent(tmp_path, "opener", "def run(m):\n    return 'SUPPORT-42'\n")
    write_llm_agent(tmp_path)
    calls = stub(
        monkeypatch,
        Completion(tool_calls=[tool_call("agent__opener", "open a ticket")]),
        Completion(content="Opened SUPPORT-42."),
    )

    registry = await Registry.create(tmp_path)
    sink = RecordingSink()
    try:
        result = await Orchestrator(registry, "Be helpful.", ModelConfig()).handle(
            Message(text="please open a ticket"), sink
        )
    finally:
        await registry.aclose()

    assert result.output == "Opened SUPPORT-42."
    assert tool_messages(calls)[0]["content"] == "SUPPORT-42"
    # It is recorded as a script result, not as a model-driven agent run.
    assert [item.agent for item in result.script_results] == ["opener"]
    assert result.script_results[0].invocation == INVOCATION_DELEGATION
    assert result.agent_results == []
    assert ("agent_start", "opener (script): open a ticket", "call_0") in sink.events
    assert ("agent_end", "opener finished", "call_0") in sink.events


async def test_a_delegated_script_sees_the_task_and_the_original_message(tmp_path, monkeypatch):
    write_script_agent(tmp_path, "probe", PROBE)
    write_llm_agent(tmp_path)
    calls = stub(
        monkeypatch,
        Completion(tool_calls=[tool_call("agent__probe", "do it", context="because")]),
        Completion(content="ok"),
    )

    registry = await Registry.create(tmp_path)
    try:
        await Orchestrator(registry, "Be helpful.", ModelConfig()).handle(
            Message(text="the user's own words"), RecordingSink()
        )
    finally:
        await registry.aclose()

    payload = json.loads(tool_messages(calls)[0]["content"])
    assert payload == {
        "invocation": INVOCATION_DELEGATION,
        "task": "do it",
        "context": "because",
        "text": "the user's own words",
    }


async def test_delegation_ignores_the_trigger_rule(tmp_path, monkeypatch):
    """The model naming the agent is the decision the rule would otherwise make."""
    write_script_agent(
        tmp_path, "gated", "def run(m):\n    return 'ran anyway'\n",
        triggerRule='text.contains("=====")',
    )
    write_llm_agent(tmp_path)
    calls = stub(
        monkeypatch,
        Completion(tool_calls=[tool_call("agent__gated", "go")]),
        Completion(content="ok"),
    )

    registry = await Registry.create(tmp_path)
    try:
        await Orchestrator(registry, "Be helpful.", ModelConfig()).handle(
            Message(text="no marker here"), RecordingSink()
        )
    finally:
        await registry.aclose()

    assert tool_messages(calls)[0]["content"] == "ran anyway"


async def test_a_delegated_script_with_send_output_posts_and_says_so(tmp_path, monkeypatch):
    write_script_agent(
        tmp_path, "teller", "def run(m):\n    return 'Ticket SUPPORT-42 is open'\n",
        send_output=True,
    )
    write_llm_agent(tmp_path)
    calls = stub(
        monkeypatch,
        Completion(tool_calls=[tool_call("agent__teller", "open it")]),
        Completion(content="Done."),
    )

    registry = await Registry.create(tmp_path)
    sink = RecordingSink()
    try:
        result = await Orchestrator(registry, "Be helpful.", ModelConfig()).handle(
            Message(text="open a ticket"), sink
        )
    finally:
        await registry.aclose()

    assert sink.messages == ["Ticket SUPPORT-42 is open"]
    assert result.script_results[0].sent_to_client is True
    content = tool_messages(calls)[0]["content"]
    assert "Ticket SUPPORT-42 is open" in content
    assert "already posted to the user" in content


async def test_a_failing_delegated_script_is_reported_to_the_model(tmp_path, monkeypatch):
    write_script_agent(tmp_path, "broken", "def run(m):\n    raise RuntimeError('boom')\n")
    write_llm_agent(tmp_path)
    calls = stub(
        monkeypatch,
        Completion(tool_calls=[tool_call("agent__broken", "go")]),
        Completion(content="I could not do that."),
    )

    registry = await Registry.create(tmp_path)
    sink = RecordingSink()
    try:
        result = await Orchestrator(registry, "Be helpful.", ModelConfig()).handle(
            Message(text="hi"), sink
        )
    finally:
        await registry.aclose()

    assert result.output == "I could not do that."
    assert "boom" in tool_messages(calls)[0]["content"]
    assert any(kind == "agent_error" for kind, _, _ in sink.events)


async def test_delegating_to_a_script_that_failed_to_load(tmp_path, monkeypatch, caplog):
    write_script_agent(tmp_path, "unloadable", "def run(:\n")
    write_llm_agent(tmp_path)
    calls = stub(
        monkeypatch,
        Completion(tool_calls=[tool_call("agent__unloadable", "go")]),
        Completion(content="recovered"),
    )

    with caplog.at_level("ERROR"):
        registry = await Registry.create(tmp_path)
    try:
        await Orchestrator(registry, "Be helpful.", ModelConfig()).handle(
            Message(text="hi"), RecordingSink()
        )
    finally:
        await registry.aclose()

    assert "not loaded at startup" in tool_messages(calls)[0]["content"]


async def test_a_script_and_an_llm_agent_can_be_delegated_in_one_turn(tmp_path, monkeypatch):
    write_script_agent(tmp_path, "opener", "def run(m):\n    return 'SUPPORT-42'\n")
    write_llm_agent(tmp_path)
    calls = stub(
        monkeypatch,
        Completion(
            tool_calls=[
                tool_call("agent__opener", "open a ticket", index=0),
                tool_call("agent__reasoner", "summarise the outage", index=1),
            ]
        ),
        Completion(content="the agent's summary"),
        Completion(content="Ticket open, here is the summary."),
    )

    registry = await Registry.create(tmp_path)
    try:
        result = await Orchestrator(registry, "Be helpful.", ModelConfig()).handle(
            Message(text="outage"), RecordingSink()
        )
    finally:
        await registry.aclose()

    assert result.output == "Ticket open, here is the summary."
    assert [item.agent for item in result.script_results] == ["opener"]
    assert [item.agent for item in result.agent_results] == ["reasoner"]
    contents = {msg["tool_call_id"]: msg["content"] for msg in tool_messages(calls)}
    assert contents == {"call_0": "SUPPORT-42", "call_1": "the agent's summary"}


# --- the after-orchestrator phase -----------------------------------------------------


AFTER_PROBE = """\
def run(message):
    return "AFTER SAW: " + (message["orchestrator_output"] or "(nothing)")
"""


async def test_the_after_phase_receives_the_orchestrator_answer(tmp_path):
    write_script_agent(tmp_path, "auditor", AFTER_PROBE, triggerPoint="after_orchestrator")

    registry = await Registry.create(tmp_path)
    try:
        phase = ScriptPhase(
            registry.script_agents_after, registry.script_runners(), TRIGGER_POINT_AFTER
        )
        results = await phase.run(
            Message(text="hi"), RecordingSink(), (), "ORCHESTRATOR ANSWER"
        )
    finally:
        await registry.aclose()

    assert results[0].output == "AFTER SAW: ORCHESTRATOR ANSWER"
    assert results[0].trigger_point == TRIGGER_POINT_AFTER
    assert results[0].invocation == INVOCATION_TRIGGER


async def test_the_before_phase_sees_no_orchestrator_output(tmp_path):
    """It has not run yet, so the key is present but empty rather than missing."""
    write_script_agent(tmp_path, "early", AFTER_PROBE, triggerPoint="before_orchestrator")

    registry = await Registry.create(tmp_path)
    try:
        phase = ScriptPhase(registry.script_agents_before, registry.script_runners())
        results = await phase.run(Message(text="hi"), RecordingSink())
    finally:
        await registry.aclose()

    assert results[0].output == "AFTER SAW: (nothing)"


PRIOR_PROBE = """\
def run(message):
    return ",".join(item["agent"] for item in message["prior_outputs"]) or "(none)"
"""


async def test_the_after_phase_sees_the_before_phase_output(tmp_path):
    write_script_agent(tmp_path, "auditor", PRIOR_PROBE, triggerPoint="after_orchestrator")
    from stark.types import ScriptResult

    registry = await Registry.create(tmp_path)
    try:
        phase = ScriptPhase(
            registry.script_agents_after, registry.script_runners(), TRIGGER_POINT_AFTER
        )
        results = await phase.run(
            Message(text="hi"), RecordingSink(), [ScriptResult(agent="opener", output="X")]
        )
    finally:
        await registry.aclose()

    assert results[0].output == "opener"
    # `prior` is context, not part of this phase's own result set.
    assert len(results) == 1


async def test_the_after_phase_runs_its_bands_in_priority_order(tmp_path):
    write_script_agent(tmp_path, "first", triggerPoint="after_orchestrator", priority=300)
    write_script_agent(tmp_path, "second", triggerPoint="after_orchestrator", priority=100)

    registry = await Registry.create(tmp_path)
    try:
        phase = ScriptPhase(
            registry.script_agents_after, registry.script_runners(), TRIGGER_POINT_AFTER
        )
        results = await phase.run(Message(text="hi"), RecordingSink())
    finally:
        await registry.aclose()

    assert [item.agent for item in results] == ["first", "second"]


async def test_a_trigger_rule_still_gates_the_after_phase(tmp_path):
    write_script_agent(
        tmp_path, "gated", triggerPoint="after_orchestrator",
        triggerRule='text.contains("=====")',
    )

    registry = await Registry.create(tmp_path)
    try:
        phase = ScriptPhase(
            registry.script_agents_after, registry.script_runners(), TRIGGER_POINT_AFTER
        )
        matched = await phase.run(Message(text="===== x ====="), RecordingSink())
        skipped = await phase.run(Message(text="ordinary"), RecordingSink())
    finally:
        await registry.aclose()

    assert [item.agent for item in matched] == ["gated"]
    assert skipped == []


# --- end to end through the runtime ---------------------------------------------------


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


async def test_an_after_agent_runs_once_the_answer_is_out(tmp_path, monkeypatch, capsys, model):
    write_script_agent(
        tmp_path, "auditor", AFTER_PROBE,
        triggerPoint="after_orchestrator", send_output=True,
    )
    write_llm_agent(tmp_path)
    feed(monkeypatch, "hello", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")
    out = capsys.readouterr().out

    assert "AFTER SAW: ORCHESTRATOR ANSWER" in out
    # The answer was delivered first; the script commented on it afterwards.
    assert out.index("ORCHESTRATOR ANSWER") < out.index("AFTER SAW")
    # Its output cannot reach the model — that turn is already finished.
    assert len(model.calls) == 1
    assert "AFTER SAW" not in json.dumps(model.calls[0]["messages"])


async def test_both_phases_run_around_the_orchestrator(tmp_path, monkeypatch, capsys, model):
    write_script_agent(
        tmp_path, "early", "def run(m):\n    return 'BEFORE RAN'\n",
        triggerPoint="before_orchestrator", send_output=True,
    )
    write_script_agent(
        tmp_path, "late", "def run(m):\n    return 'AFTER RAN'\n",
        triggerPoint="after_orchestrator", send_output=True,
    )
    write_llm_agent(tmp_path)
    feed(monkeypatch, "hello", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")
    out = capsys.readouterr().out

    assert out.index("BEFORE RAN") < out.index("ORCHESTRATOR ANSWER") < out.index("AFTER RAN")
    # Only the before-phase result is context for the model.
    user_turn = model.calls[0]["messages"][1]["content"]
    assert "BEFORE RAN" in user_turn
    assert "AFTER RAN" not in user_turn


async def test_an_after_agent_still_runs_with_no_llm_agents(
    tmp_path, monkeypatch, capsys, model
):
    """No orchestrator to run after, but the phase is still its place in the pipeline."""
    write_script_agent(
        tmp_path, "late", AFTER_PROBE, triggerPoint="after_orchestrator", send_output=True
    )
    feed(monkeypatch, "hello", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")
    out = capsys.readouterr().out

    assert model.calls == []
    assert "AFTER SAW: (nothing)" in out


async def test_the_after_phase_sees_a_delegated_script_result(tmp_path, monkeypatch, capsys, model):
    """Delegated results are part of the run, so the after phase gets them as context."""

    async def delegate_then_answer(**kwargs):
        model.calls.append(kwargs)
        if len(model.calls) == 1:
            return Completion(tool_calls=[tool_call("agent__opener", "open it")])
        return Completion(content="ORCHESTRATOR ANSWER")

    monkeypatch.setattr(
        llm_client.LLMClient, "complete", staticmethod(delegate_then_answer)
    )
    write_script_agent(tmp_path, "opener", "def run(m):\n    return 'SUPPORT-42'\n")
    write_script_agent(
        tmp_path, "auditor", PRIOR_PROBE,
        triggerPoint="after_orchestrator", send_output=True,
    )
    write_llm_agent(tmp_path)
    feed(monkeypatch, "hello", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")
    out = capsys.readouterr().out

    # PRIOR_PROBE prints the agents it saw, or "(none)".
    assert "script › opener" in out
    assert "(none)" not in out


def count_settles(monkeypatch) -> list[int]:
    """Count `settle()` calls on the CLI sink the runtime builds for each query."""
    from stark.listeners.cli import CLISink

    calls = [0]

    async def settle(self) -> None:
        calls[0] += 1

    monkeypatch.setattr(CLISink, "settle", settle, raising=False)
    return calls


async def test_the_sink_is_settled_once_the_after_phase_has_run(
    tmp_path, monkeypatch, capsys, model
):
    write_script_agent(tmp_path, "late", triggerPoint="after_orchestrator")
    write_llm_agent(tmp_path)
    settles = count_settles(monkeypatch)
    feed(monkeypatch, "hello", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")

    assert settles[0] == 1


async def test_the_sink_is_not_settled_when_nothing_ran_after(
    tmp_path, monkeypatch, capsys, model
):
    """A no-op phase must not cost an extra progress edit."""
    write_script_agent(tmp_path, "early")
    write_llm_agent(tmp_path)
    settles = count_settles(monkeypatch)
    feed(monkeypatch, "hello", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")

    assert settles[0] == 0


async def test_the_sink_is_not_settled_when_the_after_phase_did_not_trigger(
    tmp_path, monkeypatch, capsys, model
):
    write_script_agent(
        tmp_path, "gated", triggerPoint="after_orchestrator",
        triggerRule='text.contains("=====")',
    )
    write_llm_agent(tmp_path)
    settles = count_settles(monkeypatch)
    feed(monkeypatch, "an ordinary question", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")

    assert settles[0] == 0


async def test_slack_strikes_a_step_that_ran_after_the_answer(tmp_path):
    """The progress message reopens after `final`, so `settle` has to close it again."""
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

    client = FakeClient()
    sink = SlackSink(
        client, channel="C1", thread_ts="T1", config=SlackConfig(update_interval=0)
    )

    await sink.status("working")
    await sink.final("the answer")
    # Now the after-orchestrator phase reports itself.
    await sink.event("agent_start", "auditor (script)", key="auditor")
    await sink.event("agent_end", "auditor (script)", key="auditor")
    await sink.settle()

    progress = client.updates[-1]["text"]
    assert progress == ":white_check_mark: ~auditor (script)~"
    assert [item["text"] for item in client.posted[1:]] == ["the answer"]
