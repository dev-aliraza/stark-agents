"""CLI listener output, including the per-query timing footer."""

from __future__ import annotations

import re

import pytest

from stark.listeners.cli import CLIListener, CLISink, format_duration
from stark.types import AgentResult, RunResult

DURATION = re.compile(r"\d+(?:\.\d+)?\s*(?:ms|s)\b")


# --- duration formatting ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "0ms"),
        (0.0004, "0ms"),
        (0.012, "12ms"),
        (0.9994, "999ms"),
        (1.0, "1.00s"),
        (1.234, "1.23s"),
        (59.99, "59.99s"),
        (60.0, "1m 00.0s"),
        (95.4, "1m 35.4s"),
        (3723.5, "62m 03.5s"),
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_format_duration_switches_unit_at_one_second():
    assert format_duration(0.999).endswith("ms")
    assert format_duration(1.001).endswith("s")
    assert not format_duration(1.001).endswith("ms")


# --- footer composition ---------------------------------------------------------------


def test_footer_leads_with_the_elapsed_time():
    footer = CLIListener._footer(RunResult(iterations=1), 2.5)
    assert footer.startswith("2.50s")


def test_footer_includes_every_available_figure():
    result = RunResult(
        iterations=3,
        cost=0.0412,
        agent_results=[
            AgentResult(agent="sales-agent", task="t"),
            AgentResult(agent="writer-agent", task="t"),
        ],
    )
    assert CLIListener._footer(result, 12.3) == "12.30s · 3 iteration(s) · 2 agent call(s) · $0.0412"


def test_footer_omits_cost_and_agents_when_there_are_none():
    """A stubbed or cached run reports no cost; the timing must still show."""
    assert CLIListener._footer(RunResult(iterations=1), 0.25) == "250ms · 1 iteration(s)"


# --- end-to-end through the real listener loop ----------------------------------------


def feed(monkeypatch, *lines: str) -> None:
    """Replace input() with a scripted sequence."""
    queue = iter(lines)
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: next(queue))


async def test_timing_footer_is_printed_after_the_answer(monkeypatch, capsys):
    feed(monkeypatch, "what were emea sales?", "/exit")

    async def handler(message, sink):
        await sink.chunk("EMEA Q2 was $4.48M.")
        await sink.final("EMEA Q2 was $4.48M.")
        return RunResult(
            output="EMEA Q2 was $4.48M.",
            iterations=2,
            cost=0.0122,
            agent_results=[AgentResult(agent="sales-agent", task="emea q2")],
        )

    await CLIListener(handler, roster="- sales-agent").start()
    out = capsys.readouterr().out

    assert "EMEA Q2 was $4.48M." in out
    assert DURATION.search(out), f"no duration in output:\n{out}"
    assert "2 iteration(s)" in out
    assert "1 agent call(s)" in out
    assert "$0.0122" in out

    # The footer must come after the answer, not before it.
    assert out.index("EMEA Q2 was $4.48M.") < out.index("2 iteration(s)")


async def test_timing_is_reported_even_when_the_handler_raises(monkeypatch, capsys):
    feed(monkeypatch, "boom", "/exit")

    async def handler(message, sink):
        raise RuntimeError("provider exploded")

    await CLIListener(handler).start()
    out = capsys.readouterr().out

    assert "provider exploded" in out
    assert DURATION.search(out), f"a slow failure should still be timed:\n{out}"


async def test_zero_cost_run_still_shows_timing(monkeypatch, capsys):
    feed(monkeypatch, "hi", "/exit")

    async def handler(message, sink):
        await sink.final("hi")
        return RunResult(output="hi", iterations=1)

    await CLIListener(handler).start()
    out = capsys.readouterr().out

    assert DURATION.search(out)
    assert "$" not in out.split("hi", 1)[-1]


async def test_slash_commands_are_not_timed(monkeypatch, capsys):
    """/agents and /exit never reach the handler, so they get no footer."""
    called = False

    async def handler(message, sink):
        nonlocal called
        called = True
        return RunResult()

    feed(monkeypatch, "/agents", "", "/exit")
    await CLIListener(handler, roster="- sales-agent").start()
    out = capsys.readouterr().out

    assert called is False
    assert "- sales-agent" in out
    assert "iteration(s)" not in out


async def test_measured_time_reflects_actual_work(monkeypatch, capsys):
    """The footer must time the handler, not just formatting."""
    import asyncio

    feed(monkeypatch, "slow", "/exit")

    async def handler(message, sink):
        await asyncio.sleep(0.25)
        await sink.final("done")
        return RunResult(output="done", iterations=1)

    await CLIListener(handler).start()
    out = capsys.readouterr().out

    match = DURATION.search(out)
    assert match, out
    token = match.group(0)
    value = float(re.match(r"[\d.]+", token).group(0))
    seconds = value / 1000 if token.endswith("ms") else value
    assert 0.2 <= seconds < 5.0, f"measured {token}, expected roughly 0.25s"


# --- the footer is CLI-only ----------------------------------------------------------


async def test_slack_output_carries_no_timing():
    """Timing was requested for the CLI only; Slack replies must be unchanged."""
    pytest.importorskip("slack_bolt", reason="needs the [slack] extra")
    from stark.listeners.slack import SlackListener

    posted: list[str] = []

    class FakeClient:
        async def chat_postMessage(self, **kwargs):
            posted.append(kwargs.get("text", ""))
            return {"ts": "1.1"}

        async def chat_update(self, **kwargs):
            posted.append(kwargs.get("text", ""))
            return {"ok": True}

    async def handler(message, sink):
        await sink.final("EMEA Q2 was $4.48M.")
        return RunResult(output="EMEA Q2 was $4.48M.", iterations=2, cost=0.0122)

    await SlackListener(handler)._dispatch(
        {"text": "emea?", "channel": "C1", "ts": "1.0"}, FakeClient(), "app_mention"
    )

    assert any("EMEA Q2 was $4.48M." in text for text in posted)
    for text in posted:
        assert "iteration(s)" not in text
        assert not DURATION.search(text), f"Slack reply should not be timed: {text!r}"


def test_sink_does_not_time_anything():
    """Timing lives in the listener loop, so the sink stays transport-agnostic."""
    sink = CLISink()
    assert not hasattr(sink, "started_at")
