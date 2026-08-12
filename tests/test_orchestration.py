"""Orchestration loop tests with a stubbed model layer — no network calls."""

from __future__ import annotations

import pytest

from stark.listeners.base import Message, ResponseSink
from stark.llm import client as llm_client
from stark.orchestration import Orchestrator, Registry
from stark.orchestration.agent_runner import AgentRunner
from stark.types import Completion, ModelConfig, ToolCall

pytestmark = pytest.mark.asyncio

AGENT_MD = """\
---
name: {name}
description: {description}
provider: anthropic
model: claude-opus-5
max_iterations: {max_iterations}
---

{body}
"""


class RecordingSink(ResponseSink):
    def __init__(self):
        self.chunks: list[str] = []
        self.events: list[tuple[str, str]] = []
        self.final_text: str | None = None
        self.error_text: str | None = None

    async def chunk(self, text: str) -> None:
        self.chunks.append(text)

    async def event(self, kind: str, detail: str) -> None:
        self.events.append((kind, detail))

    async def final(self, text: str) -> None:
        self.final_text = text

    async def error(self, text: str) -> None:
        self.error_text = text


class StubModel:
    """Replays scripted completions and records every request it received."""

    def __init__(self, script: list[Completion]):
        self.script = list(script)
        self.calls: list[dict] = []

    async def complete(self, **kwargs) -> Completion:
        self.calls.append(kwargs)
        if not self.script:
            return Completion(content="done")
        return self.script.pop(0)


@pytest.fixture()
def agents_dir(tmp_path):
    def write(name: str, description: str, body: str = "Do the thing.", max_iterations: int = 5):
        directory = tmp_path / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "AGENT.md").write_text(
            AGENT_MD.format(
                name=name,
                description=description,
                body=body,
                max_iterations=max_iterations,
            ),
            encoding="utf-8",
        )

    write("research-agent", "Researches topics.")
    write("editor-agent", "Rewrites text.")
    return tmp_path


@pytest.fixture()
async def registry(agents_dir):
    built = await Registry.create(agents_dir)
    yield built
    await built.aclose()


def stub(monkeypatch, script: list[Completion]) -> StubModel:
    model = StubModel(script)
    monkeypatch.setattr(llm_client.LLMClient, "complete", staticmethod(model.complete))
    return model


def call(name: str, task: str, index: int = 0) -> ToolCall:
    import json

    return ToolCall(id=f"call_{index}", name=name, arguments=json.dumps({"task": task}))


async def test_registry_exposes_one_tool_per_agent(registry):
    names = {tool["function"]["name"] for tool in registry.delegation_tools()}
    assert names == {"agent__research-agent", "agent__editor-agent"}

    schema = next(
        tool for tool in registry.delegation_tools()
        if tool["function"]["name"] == "agent__research-agent"
    )
    assert schema["function"]["parameters"]["required"] == ["task"]
    assert "Researches topics." in schema["function"]["description"]


async def test_agents_get_the_workspace_toolset(registry):
    agent = registry.agents[0]
    names = {tool["function"]["name"] for tool in registry.toolbox_for(agent).schemas()}
    assert names == {"workspace_list", "workspace_read", "workspace_run"}


async def test_direct_answer_without_delegation(registry, monkeypatch):
    model = stub(monkeypatch, [Completion(content="42")])
    orchestrator = Orchestrator(registry, "Be helpful.", ModelConfig())
    sink = RecordingSink()

    result = await orchestrator.handle(Message(text="what is 6*7?"), sink)

    assert result.output == "42"
    assert result.iterations == 1
    assert result.agent_results == []
    assert sink.final_text == "42"
    assert len(model.calls) == 1


async def test_parallel_delegation_to_two_agents(registry, monkeypatch):
    model = stub(
        monkeypatch,
        [
            # Orchestrator turn 1: fan out to both agents at once.
            Completion(
                tool_calls=[
                    call("agent__research-agent", "research latency", 0),
                    call("agent__editor-agent", "rewrite it", 1),
                ]
            ),
            Completion(content="research findings"),  # research-agent
            Completion(content="rewritten text"),  # editor-agent
            Completion(content="combined answer"),  # orchestrator turn 2
        ],
    )
    orchestrator = Orchestrator(registry, "Be helpful.", ModelConfig())
    sink = RecordingSink()

    result = await orchestrator.handle(Message(text="research and rewrite"), sink)

    assert result.output == "combined answer"
    assert result.iterations == 2
    assert {item.agent for item in result.agent_results} == {"research-agent", "editor-agent"}
    assert {item.output for item in result.agent_results} == {"research findings", "rewritten text"}

    # The final orchestrator turn must carry a tool response per delegation.
    final_messages = model.calls[-1]["messages"]
    tool_messages = [msg for msg in final_messages if msg["role"] == "tool"]
    assert len(tool_messages) == 2
    assert {msg["tool_call_id"] for msg in tool_messages} == {"call_0", "call_1"}

    assert ("agent_start", "research-agent: research latency") in sink.events


async def test_agent_receives_its_agent_md_as_system_prompt(registry, monkeypatch):
    model = stub(monkeypatch, [Completion(content="ok")])
    agent = registry.agent_for("agent__research-agent")
    runner = AgentRunner(agent, registry.toolbox_for(agent))

    await runner.run("do it", "extra context", RecordingSink())

    request = model.calls[0]
    system = request["messages"][0]["content"]
    assert "Do the thing." in system
    assert "workspace_run" in system
    assert "extra context" in request["messages"][1]["content"]
    # Per-agent limits are honoured, not the orchestrator's.
    assert request["max_output_tokens"] == 4096
    assert request["model"] == "claude-opus-5"
    assert request["provider"] == "anthropic"


async def test_unknown_tool_name_is_reported_to_the_model(registry, monkeypatch):
    model = stub(
        monkeypatch,
        [
            Completion(tool_calls=[call("agent__ghost", "do something")]),
            Completion(content="recovered"),
        ],
    )
    orchestrator = Orchestrator(registry, "Be helpful.", ModelConfig())

    result = await orchestrator.handle(Message(text="hi"), RecordingSink())

    assert result.output == "recovered"
    tool_message = [msg for msg in model.calls[-1]["messages"] if msg["role"] == "tool"][0]
    assert "no agent named 'agent__ghost'" in tool_message["content"]


async def test_missing_task_argument_is_reported(registry, monkeypatch):
    model = stub(
        monkeypatch,
        [
            Completion(tool_calls=[ToolCall(id="c1", name="agent__editor-agent", arguments="{}")]),
            Completion(content="asked again"),
        ],
    )
    orchestrator = Orchestrator(registry, "Be helpful.", ModelConfig())

    await orchestrator.handle(Message(text="hi"), RecordingSink())

    tool_message = [msg for msg in model.calls[-1]["messages"] if msg["role"] == "tool"][0]
    assert "'task' is required" in tool_message["content"]


async def test_model_error_surfaces_on_the_sink(registry, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(llm_client.LLMClient, "complete", staticmethod(boom))
    orchestrator = Orchestrator(registry, "Be helpful.", ModelConfig())
    sink = RecordingSink()

    result = await orchestrator.handle(Message(text="hi"), sink)

    assert "provider exploded" in (result.error or "")
    assert "provider exploded" in (sink.error_text or "")


async def test_agent_failure_is_reported_back_not_raised(registry, monkeypatch):
    calls = {"n": 0}

    async def sometimes_fails(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return Completion(tool_calls=[call("agent__research-agent", "research")])
        if calls["n"] == 2:
            raise RuntimeError("agent model down")
        return Completion(content="handled the failure")

    monkeypatch.setattr(llm_client.LLMClient, "complete", staticmethod(sometimes_fails))
    orchestrator = Orchestrator(registry, "Be helpful.", ModelConfig())

    result = await orchestrator.handle(Message(text="hi"), RecordingSink())

    assert result.output == "handled the failure"
    assert result.agent_results[0].error is not None


async def test_orchestrator_iteration_limit(registry, monkeypatch):
    async def always_delegates(**kwargs):
        if kwargs.get("tools") and kwargs["tools"][0]["function"]["name"].startswith("agent__"):
            return Completion(
                content="still going", tool_calls=[call("agent__editor-agent", "again")]
            )
        return Completion(content="agent output")

    monkeypatch.setattr(llm_client.LLMClient, "complete", staticmethod(always_delegates))
    orchestrator = Orchestrator(registry, "Be helpful.", ModelConfig(max_iterations=2))

    result = await orchestrator.handle(Message(text="loop"), RecordingSink())

    assert result.max_iterations_reached is True
    assert result.iterations == 2


async def test_agent_iteration_limit_is_flagged_to_the_orchestrator(registry, monkeypatch):
    async def agent_always_calls_a_tool(**kwargs):
        return Completion(
            content="working",
            tool_calls=[ToolCall(id="t1", name="workspace_list", arguments="{}")],
        )

    monkeypatch.setattr(
        llm_client.LLMClient, "complete", staticmethod(agent_always_calls_a_tool)
    )
    agent = registry.agent_for("agent__editor-agent")
    result = await AgentRunner(agent, registry.toolbox_for(agent)).run("go", "", RecordingSink())

    assert result.max_iterations_reached is True
    assert result.iterations == 5  # from the agent's own max_iterations
    assert "stopped after reaching its" in result.as_tool_content()


async def test_system_prompt_carries_instructions_and_roster(registry):
    orchestrator = Orchestrator(registry, "MASTER PROMPT HERE", ModelConfig())
    prompt = orchestrator.system_prompt()

    assert "MASTER PROMPT HERE" in prompt
    assert "agent__research-agent" in prompt
    assert "Researches topics." in prompt
    assert "in parallel" in prompt


async def test_streamed_text_reaches_the_sink(registry, monkeypatch):
    async def streaming(**kwargs):
        on_text = kwargs.get("on_text")
        if on_text:
            for piece in ("Hel", "lo ", "world"):
                await on_text(piece)
        return Completion(content="Hello world")

    monkeypatch.setattr(llm_client.LLMClient, "complete", staticmethod(streaming))
    orchestrator = Orchestrator(registry, "Be helpful.", ModelConfig())
    sink = RecordingSink()

    await orchestrator.handle(Message(text="hi"), sink)

    assert sink.chunks == ["Hel", "lo ", "world"]
    assert sink.final_text == "Hello world"
