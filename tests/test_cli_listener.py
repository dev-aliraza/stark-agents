"""CLI listener output, including the per-query timing footer."""

from __future__ import annotations

import re

import pytest

from stark.listeners.cli import BasicReader, CLIListener, CLISink, format_duration
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

    await CLIListener(handler, roster="- sales-agent", reader=BasicReader()).start()
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

    await CLIListener(handler, reader=BasicReader()).start()
    out = capsys.readouterr().out

    assert "provider exploded" in out
    assert DURATION.search(out), f"a slow failure should still be timed:\n{out}"


async def test_zero_cost_run_still_shows_timing(monkeypatch, capsys):
    feed(monkeypatch, "hi", "/exit")

    async def handler(message, sink):
        await sink.final("hi")
        return RunResult(output="hi", iterations=1)

    await CLIListener(handler, reader=BasicReader()).start()
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
    await CLIListener(handler, roster="- sales-agent", reader=BasicReader()).start()
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

    await CLIListener(handler, reader=BasicReader()).start()
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


# --- reading a multi-line query -----------------------------------------------------------


PASTED = """Task: create a copy of the existing tab.

Context: the doc holds weekly updates.

## Steps to perform

- Open the Google Doc.
- Duplicate the first tab and name it with today's date."""


def feeder(*lines: str):
    """Stand in for input(), and for "is more input already buffered?"

    A paste arrives as one burst, so everything after the first line is pending until the
    queue runs dry — which is exactly what the real `select` on a tty reports.
    """
    queue = list(lines)

    def read(prompt: str = "") -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    return read, lambda: bool(queue)


def test_a_pasted_block_is_read_as_one_query(monkeypatch):
    """The bug: line 1 was submitted alone and every later line became its own query."""
    from stark.listeners.cli import read_query

    read, pending = feeder(*PASTED.splitlines())
    monkeypatch.setattr("builtins.input", read)

    assert read_query("you > ", more_pending=pending) == PASTED


def test_blank_lines_inside_a_pasted_block_survive(monkeypatch):
    from stark.listeners.cli import read_query

    read, pending = feeder("first", "", "third")
    monkeypatch.setattr("builtins.input", read)

    assert read_query("you > ", more_pending=pending) == "first\n\nthird"


def test_a_single_typed_line_is_returned_immediately(monkeypatch):
    """Nothing pending, so no waiting and no joining."""
    from stark.listeners.cli import read_query

    monkeypatch.setattr("builtins.input", lambda prompt="": "what is 6*7?")

    assert read_query("you > ", more_pending=lambda: False) == "what is 6*7?"


def test_two_separately_typed_lines_stay_separate(monkeypatch):
    """A human cannot type the next line within the settle window, so nothing is joined."""
    from stark.listeners.cli import read_query

    read, _ = feeder("first question", "second question")
    monkeypatch.setattr("builtins.input", read)

    assert read_query("you > ", more_pending=lambda: False) == "first question"
    assert read_query("you > ", more_pending=lambda: False) == "second question"


def test_a_paste_without_a_trailing_newline_still_returns(monkeypatch):
    """The queue empties mid-read; EOF ends the block rather than losing it."""
    from stark.listeners.cli import read_query

    queue = ["one", "two"]

    def read(prompt: str = "") -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", read)

    assert read_query("you > ", more_pending=lambda: True) == "one\ntwo"


def test_an_eof_on_the_first_line_propagates(monkeypatch):
    """Ctrl-D at an empty prompt must still quit, not be swallowed as an empty query."""
    from stark.listeners.cli import read_query

    def read(prompt: str = ""):
        raise EOFError

    monkeypatch.setattr("builtins.input", read)

    with pytest.raises(EOFError):
        read_query("you > ", more_pending=lambda: False)


# --- typing a block deliberately ----------------------------------------------------------


def test_a_triple_quote_opens_a_block(monkeypatch):
    from stark.listeners.cli import read_query

    read, _ = feeder('"""', "line one", "line two", '"""')
    monkeypatch.setattr("builtins.input", read)

    assert read_query("you > ", more_pending=lambda: False) == "line one\nline two"


def test_a_block_can_hold_blank_lines_and_slash_words(monkeypatch):
    """Inside a block nothing is a command — it is all prompt text."""
    from stark.listeners.cli import read_query

    read, _ = feeder('"""', "- do this", "", "/agents is just text here", '"""')
    monkeypatch.setattr("builtins.input", read)

    result = read_query("you > ", more_pending=lambda: False)
    assert result == "- do this\n\n/agents is just text here"


def test_ctrl_d_closes_an_unterminated_block(monkeypatch):
    from stark.listeners.cli import read_query

    read, _ = feeder('"""', "half written")
    monkeypatch.setattr("builtins.input", read)

    assert read_query("you > ", more_pending=lambda: False) == "half written"


# --- the pending check --------------------------------------------------------------------


def test_a_pipe_is_never_treated_as_a_paste(monkeypatch):
    """`echo ... | stark` keeps one query per line, as it always did."""
    from stark.listeners.cli import stdin_pending

    class NotATty:
        def isatty(self):
            return False

    monkeypatch.setattr("sys.stdin", NotATty())
    assert stdin_pending() is False


def test_an_unselectable_stdin_is_not_a_paste(monkeypatch):
    from stark.listeners.cli import stdin_pending

    class Broken:
        def isatty(self):
            raise ValueError("closed")

    monkeypatch.setattr("sys.stdin", Broken())
    assert stdin_pending() is False


# --- which reader gets used ----------------------------------------------------------------


def test_the_editor_reader_builds_when_prompt_toolkit_is_installed():
    """prompt_toolkit is what makes a paste wait for Enter."""
    pytest.importorskip("prompt_toolkit", reason="needs the [cli] extra")
    from stark.listeners.cli import EditorReader

    assert isinstance(EditorReader.build(), EditorReader)


def test_a_terminal_gets_the_editor(monkeypatch):
    pytest.importorskip("prompt_toolkit", reason="needs the [cli] extra")
    from stark.listeners.cli import EditorReader, build_reader

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    assert isinstance(build_reader(), EditorReader)


def test_a_pipe_gets_the_plain_reader():
    """A line editor cannot edit a pipe, and `echo ... | stark` must keep working.

    Under pytest stdin is already not a tty, which is why the whole suite exercises the
    plain reader and can monkeypatch `input`.
    """
    from stark.listeners.cli import BasicReader, build_reader

    assert isinstance(build_reader(), BasicReader)


def test_the_basic_reader_is_the_fallback(monkeypatch):
    """Without prompt_toolkit the CLI still works, it just cannot wait for review."""
    import builtins

    from stark.listeners.cli import BasicReader, build_reader

    real_import = builtins.__import__

    def no_prompt_toolkit(name, *args, **kwargs):
        if name.startswith("prompt_toolkit"):
            raise ImportError("simulated absence")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_prompt_toolkit)
    assert isinstance(build_reader(), BasicReader)


def test_only_the_editor_claims_to_hold_a_paste():
    """The banner promises different things, so the flag has to be honest."""
    pytest.importorskip("prompt_toolkit", reason="needs the [cli] extra")
    from stark.listeners.cli import BasicReader, EditorReader

    assert EditorReader.multiline_paste is True
    assert BasicReader.multiline_paste is False


def test_each_reader_explains_itself_in_the_banner():
    pytest.importorskip("prompt_toolkit", reason="needs the [cli] extra")
    from stark.listeners.cli import BasicReader, EditorReader

    assert "press Enter to send" in EditorReader.build().hint()
    # The fallback says what it cannot do rather than implying the same behaviour.
    assert "prompt_toolkit" in BasicReader().hint()


async def test_the_banner_carries_the_reader_hint():
    from stark.listeners.cli import BasicReader, CLIListener

    async def handler(message, sink):
        return RunResult()

    banner = CLIListener(handler, reader=BasicReader())._banner()
    assert BasicReader().hint() in banner


async def test_alt_enter_inserts_a_newline_without_sending():
    """Typing a block by hand needs a key that is not Enter."""
    pytest.importorskip("prompt_toolkit", reason="needs the [cli] extra")
    from stark.listeners.cli import EditorReader

    reader = EditorReader.build()
    bindings = reader.session.key_bindings.bindings
    keys = {tuple(str(key) for key in binding.keys) for binding in bindings}

    assert ("Keys.Escape", "Keys.ControlM") in keys or any(
        "Escape" in " ".join(pair) for pair in keys
    ), keys
