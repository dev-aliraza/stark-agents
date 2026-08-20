"""What a sub-agent is doing, as it reaches the CLI.

The CLI used to print which tool an agent reached for and nothing else — `browser_click_at`
told you a click happened, not that it landed on "Delete column"; `browser_scroll` told you
nothing about whether the page moved. Watching an agent work is most of how you tell a stuck
run from a slow one, so the arguments and the outcome are worth the characters.
"""

from __future__ import annotations

import io
import json

import pytest

from stark.listeners.cli import CLISink
from stark.orchestration.narrate import describe_call, describe_result


# --- summarising a call ---------------------------------------------------------------------


def test_the_salient_argument_leads():
    assert describe_call("browser_click_text", {"tabId": 1, "text": "Insert column left"}) == (
        'browser_click_text "Insert column left"'
    )


def test_the_tab_id_is_left_out():
    """It is the same for every call in a run, so it is pure noise."""
    assert "tabId" not in describe_call("browser_screenshot", {"tabId": 42})


def test_coordinates_and_modifiers_are_shown():
    described = describe_call(
        "browser_click_at", {"tabId": 1, "x": 500, "y": 620, "modifiers": ["shift"]}
    )
    assert "x=500" in described and "y=620" in described and "shift" in described


def test_a_secret_is_never_echoed():
    """A progress line goes to a terminal and to Slack; neither should carry a token."""
    described = describe_call("shell_run", {"command": "curl x", "token": "hunter2"})
    assert "hunter2" not in described and "token=…" in described


def test_a_long_value_is_truncated():
    described = describe_call("browser_open", {"url": "https://example.com/" + "x" * 400})
    assert len(described) < 120


def test_a_call_with_no_arguments_is_just_its_name():
    assert describe_call("browser_tabs", {}) == "browser_tabs"


# --- summarising a result --------------------------------------------------------------------


def test_the_interesting_fields_are_picked_out():
    summary = describe_result(json.dumps({"clicked": "Insert column left", "matched": "exact"}))
    assert "Insert column left" in summary and "exact" in summary


def test_a_scroll_reports_whether_it_moved():
    assert "scrolled=600" in describe_result(json.dumps({"scrolled": 600, "atEnd": False}))
    assert "atEnd=True" in describe_result(json.dumps({"scrolled": 0, "atEnd": True}))


def test_an_error_survives_intact():
    assert describe_result("[error] nothing has that text").startswith("[error]")


def test_a_screenshot_never_leaks_its_image():
    """The image is carried separately, but a note or a stray field must not reach a terminal."""
    summary = describe_result(json.dumps({
        "tabId": 42, "width": 1400, "height": 658,
        "note": "a very long instruction " * 40,
        "image": "data:image/png;base64," + "A" * 5000,
    }))
    assert "base64" not in summary and "AAAA" not in summary
    assert len(summary) < 130
    assert "width=1400" in summary


def test_matches_are_counted_and_named():
    summary = describe_result(json.dumps({"matches": [{"label": "Copy"}, {"label": "Copy all"}]}))
    assert "2 match(es)" in summary and "Copy" in summary


def test_nothing_worth_saying_returns_nothing():
    """The caller stays quiet rather than printing a line that only says it finished."""
    assert describe_result("{}") == ""
    assert describe_result("") == ""


def test_plain_text_is_reduced_to_one_line():
    summary = describe_result("first line\nsecond line\nthird line")
    assert summary == "first line"


# --- how it renders -------------------------------------------------------------------------


async def render(events) -> str:
    """Drive the CLI the way the runner does: `event` for agents, `detail` for tools."""
    sink = CLISink(show_events=True)
    sink.color = False
    captured = io.StringIO()
    sink._write = lambda text, newline=False: captured.write(f"{text}\n" if newline else text)
    for kind, text, key in events:
        if kind in {"tool", "tool_end", "agent_say"}:
            await sink.detail(kind, text, key=key)
        else:
            await sink.event(kind, text, key=key)
    return captured.getvalue()


async def test_a_tool_shows_its_call_then_its_outcome():
    out = await render([
        ("tool", "a → browser_scroll amount=600", "k1"),
        ("tool_end", "scrolled=600 atEnd=False", "k1"),
    ])
    lines = out.strip().splitlines()

    assert "browser_scroll amount=600" in lines[0]
    # The call is on the line above, so repeating it to append the result is unreadable.
    assert lines[1].strip() == "✓ scrolled=600 atEnd=False"


async def test_a_tool_with_nothing_to_report_prints_one_line():
    # `describe_result` returns "" when there is nothing to add beyond "it finished".
    out = await render([
        ("tool", "a → browser_close", "k1"),
        ("tool_end", "", "k1"),
    ])
    assert len(out.strip().splitlines()) == 1


async def test_a_failed_outcome_is_marked_as_one():
    out = await render([
        ("tool", "a → browser_click_text", "k1"),
        ("tool_end", "[error] nothing has that text", "k1"),
    ])
    assert "✗" in out and "✓" not in out


async def test_the_agents_own_words_are_shown():
    """Its instructions tell it to name the checklist item it is on; that is the progress."""
    out = await render([("agent_say", "vision-agent: Item 2 of 4: adding the column", None)])
    assert "Item 2 of 4" in out


async def test_a_multi_line_checklist_keeps_its_indent():
    out = await render([("agent_say", "a: Checklist:\n1. Open\n2. Edit", None)])
    lines = out.rstrip().splitlines()

    assert len(lines) == 3
    # Continuation lines stay in the column, or the list stops reading as part of the run.
    assert lines[1].startswith("      ") and lines[2].startswith("      ")


async def test_an_empty_event_prints_nothing():
    assert await render([("tool_end", "   ", "k1")]) == ""


async def test_events_can_be_switched_off():
    sink = CLISink(show_events=False)
    captured = io.StringIO()
    sink._write = lambda text, newline=False: captured.write(text)
    await sink.event("tool", "a → browser_scroll", "k1")
    assert captured.getvalue() == ""


# --- the CLI only ---------------------------------------------------------------------------


def test_the_base_sink_ignores_narration():
    """A sink opts in by overriding `detail`; nothing changes for one that does not."""
    from stark.listeners.base import ResponseSink

    assert "detail" in vars(ResponseSink), "the hook must exist to be overridable"
    # A no-op default, so no existing sink starts rendering anything new.
    assert ResponseSink.detail.__doc__


async def test_slack_does_not_render_narration():
    """The verbose lines suit a terminal, not a chat channel that keeps one line per step."""
    pytest.importorskip("slack_bolt")
    from stark.listeners.slack import SlackSink

    assert "detail" not in vars(SlackSink), "Slack must inherit the no-op, not implement it"


async def test_slack_still_gets_the_plain_tool_events():
    """Narration is additional, not a replacement — `event` must keep its old, tidy detail."""
    from stark.orchestration.agent_runner import AgentRunner
    from stark.types import AgentConfig, ToolCall

    class Recorder:
        def __init__(self):
            self.events: list[tuple[str, str]] = []
            self.details: list[tuple[str, str]] = []

        async def event(self, kind, detail, key=None):
            self.events.append((kind, detail))

        async def detail(self, kind, text, key=None):
            self.details.append((kind, text))

    class Toolset:
        def schemas(self):
            return [{"type": "function",
                     "function": {"name": "look", "description": "", "parameters": {}}}]

        def owns(self, name):
            return name == "look"

        async def call(self, name, arguments):
            return '{"clicked": "Insert column left"}'

        async def aclose(self):
            return None

    from stark.orchestration import ToolBox
    from stark.tools import ToolFilter

    sink = Recorder()
    runner = AgentRunner(
        AgentConfig(name="a", description="d", instructions="", path="."),
        ToolBox([(Toolset(), ToolFilter())], None),
    )
    await runner._run_tools(
        [ToolCall(id="c1", name="look", arguments='{"text": "Insert column left"}')], sink, "k"
    )

    # What Slack sees: the tool name, and nothing about arguments or results.
    plain = [detail for kind, detail in sink.events if kind == "tool"]
    assert plain == ["a → look"]
    assert not any("Insert column left" in detail for _, detail in sink.events)

    # What the CLI sees: the argument on the way in, the outcome on the way back.
    assert any("Insert column left" in text for kind, text in sink.details if kind == "tool")
    assert any("clicked" in text for kind, text in sink.details if kind == "tool_end")
