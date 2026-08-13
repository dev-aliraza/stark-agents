"""Runtime wiring: script phase, then the orchestrator only if it has somewhere to route.

Driven through the real CLI listener with piped input, so the `handle` closure in
runtime.py is genuinely exercised rather than reimplemented in the test.
"""

from __future__ import annotations

import pytest

import stark
from stark.llm import client as llm_client
from stark.types import Completion

SCRIPT = """\
def run(message):
    return "SCRIPT SAW: " + message["text"]
"""


def write_script_agent(root, name: str, *, send_output: bool, trigger: str | None = None) -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "handler.py").write_text(SCRIPT, encoding="utf-8")
    # `triggerPoint` is what opts the agent into running on its own, which is the whole
    # subject of this file.
    extra = f"send_output: {str(send_output).lower()}\ntriggerPoint: before_orchestrator\n"
    if trigger:
        extra += f"triggerRule: '{trigger}'\n"
    (directory / "AGENT.md").write_text(
        f"---\nname: {name}\ndescription: Deterministic step.\n"
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


@pytest.fixture()
def model(monkeypatch):
    """Stub the single model call path and record every request."""

    class Recorder:
        def __init__(self):
            self.calls: list[dict] = []

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            return Completion(content="ORCHESTRATOR ANSWER")

    recorder = Recorder()
    monkeypatch.setattr(
        llm_client.LLMClient, "complete", staticmethod(recorder.complete)
    )
    return recorder


def feed(monkeypatch, *lines: str) -> None:
    queue = iter(lines)
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: next(queue))


async def test_script_output_is_posted_and_the_orchestrator_still_runs(
    tmp_path, monkeypatch, capsys, model
):
    write_script_agent(tmp_path, "teller", send_output=True)
    write_llm_agent(tmp_path)
    feed(monkeypatch, "hello there", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")
    out = capsys.readouterr().out

    # Both the script output and the orchestrator answer reach the user.
    assert "SCRIPT SAW: hello there" in out
    assert "ORCHESTRATOR ANSWER" in out
    assert len(model.calls) == 1

    # The script result is handed to the model, framed as already seen.
    user_turn = model.calls[0]["messages"][1]["content"]
    assert "SCRIPT SAW: hello there" in user_turn
    assert "already shown to the user" in user_turn


async def test_internal_script_output_reaches_the_model_but_not_the_user(
    tmp_path, monkeypatch, capsys, model
):
    write_script_agent(tmp_path, "quiet", send_output=False)
    write_llm_agent(tmp_path)
    feed(monkeypatch, "hello there", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")
    out = capsys.readouterr().out

    assert "SCRIPT SAW" not in out
    assert "ORCHESTRATOR ANSWER" in out

    user_turn = model.calls[0]["messages"][1]["content"]
    assert "SCRIPT SAW: hello there" in user_turn
    assert "the user has not seen this" in user_turn


async def test_no_llm_agents_means_no_model_call_at_all(
    tmp_path, monkeypatch, capsys, model
):
    write_script_agent(tmp_path, "teller", send_output=True)
    feed(monkeypatch, "hello there", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")
    out = capsys.readouterr().out

    assert model.calls == []
    assert "SCRIPT SAW: hello there" in out
    # Nothing invents an answer, and no "(no output)" placeholder is printed.
    assert "no output" not in out


async def test_untriggered_script_is_skipped_but_the_orchestrator_answers(
    tmp_path, monkeypatch, capsys, model
):
    write_script_agent(tmp_path, "gated", send_output=True, trigger='text.contains("=====")')
    write_llm_agent(tmp_path)
    feed(monkeypatch, "an ordinary question", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")
    out = capsys.readouterr().out

    assert "SCRIPT SAW" not in out
    assert "ORCHESTRATOR ANSWER" in out
    # With no script results, the user turn is just the question.
    assert model.calls[0]["messages"][1]["content"] == "an ordinary question"


async def test_triggered_script_fires_on_a_match(tmp_path, monkeypatch, capsys, model):
    write_script_agent(tmp_path, "gated", send_output=True, trigger='text.contains("=====")')
    write_llm_agent(tmp_path)
    feed(monkeypatch, "===== outage =====", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")
    out = capsys.readouterr().out

    assert "SCRIPT SAW: ===== outage =====" in out


async def test_agents_with_no_llm_and_no_match_produce_nothing(
    tmp_path, monkeypatch, capsys, model
):
    """The deliberately silent case: seen, nothing to do."""
    write_script_agent(tmp_path, "gated", send_output=True, trigger='text.contains("=====")')
    feed(monkeypatch, "an ordinary question", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")
    out = capsys.readouterr().out

    assert model.calls == []
    assert "SCRIPT SAW" not in out
    assert "no output" not in out


async def test_a_failing_script_does_not_stop_the_orchestrator(
    tmp_path, monkeypatch, capsys, model
):
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "handler.py").write_text(
        "def run(message):\n    raise RuntimeError('boom')\n", encoding="utf-8"
    )
    (directory / "AGENT.md").write_text(
        "---\nname: broken\ndescription: Fails.\ntype: script\nscript: handler.py\n"
        "triggerPoint: before_orchestrator\n---\n\nBody.\n",
        encoding="utf-8",
    )
    write_llm_agent(tmp_path)
    feed(monkeypatch, "hello", "/exit")

    await stark.run_async(agents=str(tmp_path), listener="cli")
    out = capsys.readouterr().out

    assert "ORCHESTRATOR ANSWER" in out
    # The failure is reported to the user as a step and to the model as context.
    assert "broken" in out
    assert "boom" in model.calls[0]["messages"][1]["content"]
