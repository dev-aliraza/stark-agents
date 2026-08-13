"""Slack listener behaviour: progress is streamed, the answer is posted once."""

from __future__ import annotations

import asyncio

import pytest

from stark.errors import ListenerError
from stark.listeners import validate_listener
from stark.config import SlackConfig
from stark.listeners.slack import (
    DONE_EMOJI,
    FAILED_EMOJI,
    RUNNING_EMOJI,
    STARTING_LABEL,
    SlackListener,
    SlackSink,
)
from stark.types import RunResult

pytest.importorskip("slack_bolt", reason="the slack listener needs the [slack] extra")


class FakeSlackClient:
    """Records API calls instead of talking to Slack."""

    def __init__(self):
        self.posted: list[dict] = []
        self.updates: list[dict] = []
        self._next_ts = 1000

    async def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        self._next_ts += 1
        return {"ts": f"{self._next_ts}.0001"}

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}

    @property
    def progress(self) -> str:
        """The latest rendering of the progress message."""
        if self.updates:
            return self.updates[-1]["text"]
        return self.posted[0]["text"] if self.posted else ""

    @property
    def answer(self) -> str:
        """The last posted message — the answer, when one was sent."""
        return self.posted[-1]["text"] if self.posted else ""


async def drain() -> None:
    """Let the coalescing flush task run."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.fixture()
def instant_flush() -> SlackConfig:
    """A config with no coalescing cooldown.

    Needed only by tests that assert a mid-sequence transition: draining between two
    events starts the real cooldown, so the second render would otherwise wait
    `update_interval` seconds.
    """
    return SlackConfig(update_interval=0)


def sink_for(client: FakeSlackClient, config: SlackConfig | None = None) -> SlackSink:
    return SlackSink(client, channel="C1", thread_ts="T1", config=config)


# --- the answer is never streamed -----------------------------------------------------


async def test_chunks_are_not_sent_to_slack():
    client = FakeSlackClient()
    sink = sink_for(client)

    for piece in ("The ", "answer ", "is ", "42."):
        await sink.chunk(piece)

    assert client.posted == []
    assert client.updates == []


async def test_final_answer_is_posted_once_as_its_own_message():
    client = FakeSlackClient()
    sink = sink_for(client)

    await sink.status("working")
    await sink.event("agent_start", "sales-agent: emea q2", key="c1")
    await drain()
    await sink.event("agent_end", "sales-agent finished", key="c1")
    await sink.final("EMEA Q2 was $4.48M.")

    answers = [call for call in client.posted if call["text"] == "EMEA Q2 was $4.48M."]
    assert len(answers) == 1
    assert answers[0]["thread_ts"] == "T1"
    # The answer is a separate message, not an edit of the progress one.
    assert all(call["text"] != "EMEA Q2 was $4.48M." for call in client.updates)


async def test_empty_answer_posts_nothing_and_settles_the_progress():
    """Silence is valid: it's what happens when no llm agents are registered."""
    client = FakeSlackClient()
    sink = sink_for(client)

    await sink.status("working")
    await sink.final("   ")

    # No answer message at all — the settled progress message is the whole reply.
    assert len(client.posted) == 1
    assert client.progress == f"{DONE_EMOJI} ~{STARTING_LABEL}~"
    assert sink.answer_ts is None


# --- progress streaming: loading, then struck through ---------------------------------


async def test_agent_step_shows_loading_then_struck_done(instant_flush):
    client = FakeSlackClient()
    sink = sink_for(client, instant_flush)

    await sink.event("agent_start", "sales-agent: emea q2", key="c1")
    await drain()
    assert client.progress == f"{RUNNING_EMOJI} sales-agent: emea q2"

    await sink.event("agent_end", "sales-agent finished", key="c1")
    await drain()
    assert client.progress == f"{DONE_EMOJI} ~sales-agent: emea q2~"


async def test_tool_step_shows_loading_then_struck_done(instant_flush):
    client = FakeSlackClient()
    sink = sink_for(client, instant_flush)

    await sink.event("tool", "sales-agent → workspace_run", key="c1:t1")
    await drain()
    assert client.progress == f"{RUNNING_EMOJI} sales-agent → workspace_run"

    await sink.event("tool_end", "sales-agent → workspace_run", key="c1:t1")
    await drain()
    assert client.progress == f"{DONE_EMOJI} ~sales-agent → workspace_run~"


async def test_completion_strikes_the_original_label_not_the_end_detail():
    """The struck line must keep the task text, not be replaced by 'X finished'."""
    client = FakeSlackClient()
    sink = sink_for(client)

    await sink.event("agent_start", "sales-agent: research emea sales", key="c1")
    await sink.event("agent_end", "sales-agent finished", key="c1")
    await drain()

    assert client.progress == f"{DONE_EMOJI} ~sales-agent: research emea sales~"
    assert "finished" not in client.progress


async def test_agent_error_marks_the_step_failed():
    client = FakeSlackClient()
    sink = sink_for(client)

    await sink.event("agent_start", "sales-agent: emea q2", key="c1")
    await sink.event("agent_error", "sales-agent: model exploded", key="c1")
    await drain()

    assert client.progress == f"{FAILED_EMOJI} ~sales-agent: emea q2~"


async def test_steps_accumulate_in_order():
    client = FakeSlackClient()
    sink = sink_for(client)

    await sink.event("agent_start", "sales-agent: figures", key="c1")
    await sink.event("agent_start", "writer-agent: prose", key="c2")
    await sink.event("agent_end", "sales-agent finished", key="c1")
    await drain()

    assert client.progress.splitlines() == [
        f"{DONE_EMOJI} ~sales-agent: figures~",
        f"{RUNNING_EMOJI} writer-agent: prose",
    ]


async def test_the_same_agent_twice_gets_two_independent_lines():
    """Two delegations to one agent must not collapse — this is why keys exist."""
    client = FakeSlackClient()
    sink = sink_for(client)

    await sink.event("agent_start", "sales-agent: emea", key="call_0")
    await sink.event("agent_start", "sales-agent: apac", key="call_1")
    await sink.event("agent_end", "sales-agent finished", key="call_0")
    await drain()

    assert client.progress.splitlines() == [
        f"{DONE_EMOJI} ~sales-agent: emea~",
        f"{RUNNING_EMOJI} sales-agent: apac",
    ]


async def test_tool_steps_nest_under_the_agent_that_ran_them():
    """Tool keys are "<agent key>:<call id>", so a tool sits under its own agent."""
    client = FakeSlackClient()
    sink = sink_for(client)

    await sink.event("agent_start", "sales-agent: figures", key="c1")
    await sink.event("agent_start", "inventory-agent: stock", key="c2")
    # Tools arrive after both agents started, but must not be listed after both.
    await sink.event("tool", "sales-agent → workspace_run", key="c1:t1")
    await sink.event("tool", "inventory-agent → check_stock", key="c2:t2")
    await drain()

    assert client.progress.splitlines() == [
        f"{RUNNING_EMOJI} sales-agent: figures",
        f"        ↳ {RUNNING_EMOJI} sales-agent → workspace_run",
        f"{RUNNING_EMOJI} inventory-agent: stock",
        f"        ↳ {RUNNING_EMOJI} inventory-agent → check_stock",
    ]


async def test_several_tools_from_one_agent_stay_grouped():
    client = FakeSlackClient()
    sink = sink_for(client)

    await sink.event("agent_start", "a: task", key="c1")
    await sink.event("tool", "a → one", key="c1:t1")
    await sink.event("tool", "a → two", key="c1:t2")
    await drain()

    lines = client.progress.splitlines()
    assert lines[0].startswith(RUNNING_EMOJI)
    assert lines[1].lstrip().endswith("a → one")
    assert lines[2].lstrip().endswith("a → two")
    assert all(line.startswith("        ↳") for line in lines[1:])


async def test_tool_without_a_known_parent_stays_top_level():
    client = FakeSlackClient()
    sink = sink_for(client)

    await sink.event("tool", "orphan → thing", key="nobody:t1")
    await drain()

    assert client.progress == f"{RUNNING_EMOJI} orphan → thing"


async def test_repeated_start_for_one_key_does_not_duplicate():
    client = FakeSlackClient()
    sink = sink_for(client)

    await sink.event("agent_start", "sales-agent: emea", key="c1")
    await sink.event("agent_start", "sales-agent: emea", key="c1")
    await drain()

    assert len(client.progress.splitlines()) == 1


async def test_end_without_a_matching_start_is_still_recorded():
    client = FakeSlackClient()
    sink = sink_for(client)

    await sink.event("tool_end", "orphan → thing", key="unknown")
    await drain()

    assert client.progress == f"{DONE_EMOJI} ~orphan → thing~"


# --- nothing is left spinning ---------------------------------------------------------


async def test_final_settles_any_step_still_running():
    client = FakeSlackClient()
    sink = sink_for(client)

    await sink.event("agent_start", "sales-agent: emea", key="c1")
    await sink.final("done")

    assert RUNNING_EMOJI not in client.progress
    assert client.progress == f"{DONE_EMOJI} ~sales-agent: emea~"


async def test_error_settles_running_steps_as_failed_and_posts_the_error():
    client = FakeSlackClient()
    sink = sink_for(client)

    await sink.event("agent_start", "sales-agent: emea", key="c1")
    await sink.error("provider exploded")

    assert client.progress == f"{FAILED_EMOJI} ~sales-agent: emea~"
    assert client.answer == f"{FAILED_EMOJI} provider exploded"


async def test_run_with_no_delegation_still_closes_its_placeholder():
    client = FakeSlackClient()
    sink = sink_for(client)

    await sink.status("working")
    assert client.progress == f"{RUNNING_EMOJI} {STARTING_LABEL}"

    await sink.final("42")

    assert client.progress == f"{DONE_EMOJI} ~{STARTING_LABEL}~"
    assert client.answer == "42"


# --- label hygiene -------------------------------------------------------------------


async def test_multiline_task_is_flattened_onto_one_line():
    client = FakeSlackClient()
    sink = sink_for(client)

    await sink.event("agent_start", "sales-agent: line one\nline two\n\tline three", key="c1")
    await drain()

    assert client.progress == f"{RUNNING_EMOJI} sales-agent: line one line two line three"
    assert "\n" not in client.progress


async def test_tildes_in_a_task_cannot_break_strikethrough():
    client = FakeSlackClient()
    sink = sink_for(client)

    await sink.event("agent_start", "agent: rewrite ~this~ text", key="c1")
    await sink.event("agent_end", "agent finished", key="c1")
    await drain()

    body = client.progress.removeprefix(f"{DONE_EMOJI} ")
    assert body.startswith("~") and body.endswith("~")
    assert "~" not in body[1:-1]


async def test_very_long_labels_are_truncated():
    client = FakeSlackClient()
    sink = sink_for(client)

    await sink.event("agent_start", "agent: " + "x" * 500, key="c1")
    await drain()

    assert len(client.progress) < 260
    assert client.progress.endswith("…")


# --- resilience ----------------------------------------------------------------------


async def test_updates_are_coalesced_not_one_per_event():
    """Ten rapid steps must not mean ten chat.update calls."""
    client = FakeSlackClient()
    sink = sink_for(client)

    for index in range(10):
        await sink.event("tool", f"agent → tool{index}", key=f"t{index}")
    await sink.final("done")

    assert len(client.updates) < 10
    # The settled render still lists every step.
    assert len(client.progress.splitlines()) == 10
    assert RUNNING_EMOJI not in client.progress


async def test_failed_edit_does_not_abort_the_run(caplog):
    class BrokenClient(FakeSlackClient):
        async def chat_update(self, **kwargs):
            raise RuntimeError("rate limited")

    client = BrokenClient()
    sink = sink_for(client)
    await sink.status("working")

    with caplog.at_level("WARNING"):
        await sink.event("agent_start", "a: t", key="c1")
        await drain()
        await sink.final("answer")  # must not raise

    assert "chat_update failed" in caplog.text
    assert client.answer == "answer"


# --- dispatch -------------------------------------------------------------------------


async def test_dispatch_strips_the_mention_and_threads_the_reply():
    seen: list = []

    async def handler(message, sink):
        seen.append(message)
        await sink.final("done")
        return RunResult(output="done")

    client = FakeSlackClient()
    await SlackListener(handler)._dispatch(
        {
            "text": "<@U0BOT> research checkout latency",
            "channel": "C123",
            "user": "U999",
            "ts": "1700000000.0002",
        },
        client,
        "app_mention",
    )

    assert seen[0].text == "research checkout latency"
    assert seen[0].channel == "C123"
    assert seen[0].user == "U999"
    assert seen[0].thread == "1700000000.0002"
    assert client.answer == "done"


async def test_dispatch_ignores_empty_text():
    called = False

    async def handler(message, sink):
        nonlocal called
        called = True
        return RunResult()

    await SlackListener(handler)._dispatch(
        {"text": "<@U0BOT>", "channel": "C1", "ts": "1"}, FakeSlackClient(), "app_mention"
    )
    assert called is False


async def test_dispatch_reports_handler_errors_to_slack(caplog):
    async def handler(message, sink):
        raise RuntimeError("boom")

    client = FakeSlackClient()
    with caplog.at_level("ERROR"):
        await SlackListener(handler)._dispatch(
            {"text": "hi", "channel": "C1", "ts": "1"}, client, "app_mention"
        )

    assert FAILED_EMOJI in client.answer
    assert "boom" in client.answer


# --- listener validation --------------------------------------------------------------


async def test_validate_listener_requires_both_tokens(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)

    with pytest.raises(ListenerError, match="SLACK_APP_TOKEN"):
        validate_listener("slack")


async def test_validate_listener_accepts_configured_slack(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-y")
    assert validate_listener("SLACK") == "slack"


async def test_validate_listener_rejects_unknown_names():
    with pytest.raises(ListenerError, match="unknown listener"):
        validate_listener("ftp")


# --- startup diagnostics --------------------------------------------------------------


class AuthResponse(dict):
    """Mimics slack_sdk's SlackResponse: mapping plus `.headers`."""

    def __init__(self, data: dict, headers: dict):
        super().__init__(data)
        self.headers = headers


def dm_listener(handler):
    """DMs are off by default, so a test about them has to ask for them."""
    return SlackListener(handler, SlackConfig(events={"message.im": True}))


def listener_with_auth(auth_result, config=None):
    listener = SlackListener(handler=None, config=config)

    class FakeApp:
        class client:
            @staticmethod
            async def auth_test():
                if isinstance(auth_result, Exception):
                    raise auth_result
                return auth_result

    listener._app = FakeApp()
    return listener


IDENTITY = {"user": "starkbot", "user_id": "U1", "team": "stark"}


async def test_rejected_token_is_reported_as_an_error(caplog):
    listener = listener_with_auth(RuntimeError("invalid_auth"))

    with caplog.at_level("ERROR"):
        await listener._log_identity()

    assert "token is rejected" in caplog.text
    assert "invalid_auth" in caplog.text


async def test_identity_is_logged_so_you_know_which_workspace(caplog):
    listener = listener_with_auth(
        AuthResponse(IDENTITY, {"x-oauth-scopes": "app_mentions:read,chat:write,im:history"})
    )

    with caplog.at_level("INFO"):
        await listener._log_identity()

    assert "starkbot" in caplog.text
    assert "stark" in caplog.text
    assert "scopes OK" in caplog.text


async def test_missing_scopes_are_named_with_the_reinstall_hint(caplog):
    listener = listener_with_auth(
        AuthResponse(IDENTITY, {"x-oauth-scopes": "chat:write,commands"}),
        config=SlackConfig(events={"app_mention": True, "message.im": True}),
    )

    with caplog.at_level("ERROR"):
        await listener._log_identity()

    assert "app_mentions:read" in caplog.text
    assert "im:history" in caplog.text
    assert "REINSTALL" in caplog.text
    # A granted scope must not be reported as missing.
    assert "missing the scope(s) app_mentions:read, im:history" in caplog.text


async def test_only_the_scopes_the_configured_events_need_are_required(caplog):
    """The default config never asks for im:history, so it must not be reported missing."""
    listener = listener_with_auth(
        AuthResponse(IDENTITY, {"x-oauth-scopes": "chat:write,app_mentions:read"})
    )

    with caplog.at_level("INFO"):
        await listener._log_identity()

    assert "missing the scope" not in caplog.text
    assert "scopes OK" in caplog.text
    assert "im:history" not in caplog.text.split("Direct messages")[0]


async def test_a_channel_event_adds_its_own_history_scope(caplog):
    listener = listener_with_auth(
        AuthResponse(IDENTITY, {"x-oauth-scopes": "chat:write,app_mentions:read"}),
        config=SlackConfig(events={"app_mention": True, "message.channels": True}),
    )

    with caplog.at_level("ERROR"):
        await listener._log_identity()

    assert "missing the scope(s) channels:history" in caplog.text


async def test_the_startup_log_names_what_is_listened_to(caplog):
    listener = listener_with_auth(
        AuthResponse(IDENTITY, {"x-oauth-scopes": "chat:write,app_mentions:read"}),
        config=SlackConfig(
            events={"app_mention": True, "message.channels": 'text.contains("=====")'}
        ),
    )

    with caplog.at_level("INFO"):
        await listener._log_identity()

    assert "listening for: app_mention, message.channels when" in caplog.text
    assert 'text.contains("=====")' in caplog.text


async def test_the_startup_log_points_out_that_dms_are_ignored(caplog):
    listener = listener_with_auth(
        AuthResponse(IDENTITY, {"x-oauth-scopes": "chat:write,app_mentions:read"})
    )

    with caplog.at_level("INFO"):
        await listener._log_identity()

    assert "Direct messages are ignored" in caplog.text
    assert "message.im" in caplog.text


async def test_no_dm_hint_when_dms_are_enabled(caplog):
    listener = listener_with_auth(
        AuthResponse(IDENTITY, {"x-oauth-scopes": "chat:write,im:history"}),
        config=SlackConfig(events={"message.im": True}),
    )

    with caplog.at_level("INFO"):
        await listener._log_identity()

    assert "Direct messages are ignored" not in caplog.text


async def test_the_bot_user_id_is_remembered_for_self_and_dedup_checks(caplog):
    listener = listener_with_auth(AuthResponse(IDENTITY, {}))

    await listener._log_identity()

    assert listener.bot_user_id == "U1"


async def test_scope_check_is_skipped_when_slack_reports_no_scopes(caplog):
    listener = listener_with_auth(AuthResponse(IDENTITY, {}))

    with caplog.at_level("INFO"):
        await listener._log_identity()

    assert "starkbot" in caplog.text
    assert "missing the scope" not in caplog.text


def test_granted_scopes_parsing():
    parse = SlackListener._granted_scopes
    assert parse(AuthResponse({}, {"x-oauth-scopes": "a:read, b:write"})) == {"a:read", "b:write"}
    # Header casing varies by client.
    assert parse(AuthResponse({}, {"X-OAuth-Scopes": "a:read"})) == {"a:read"}
    # Some clients expose header values as lists.
    assert parse(AuthResponse({}, {"x-oauth-scopes": ["a:read,b:write"]})) == {"a:read", "b:write"}
    assert parse(AuthResponse({}, {})) is None


async def test_an_unconfigured_message_event_is_dropped_with_a_reason(caplog):
    """Dropping it silently is what makes a quiet bot hard to diagnose."""
    import logging

    from stark.logger import configure_logging

    configure_logging(logging.DEBUG)
    try:
        called = False

        async def handler(message, sink):
            nonlocal called
            called = True
            return RunResult()

        listener = SlackListener(handler)
        with caplog.at_level("DEBUG"):
            await listener.on_message_event(
                {"text": "hello", "channel": "C1", "ts": "1", "channel_type": "channel"},
                FakeSlackClient(),
            )

        assert called is False
        assert "Ignoring message.channels: not enabled" in caplog.text
        assert "config.slack.events" in caplog.text
    finally:
        configure_logging(logging.INFO)


async def test_a_dm_is_ignored_by_default():
    """`events` defaults to app_mention alone, so a DM reaches nothing."""
    called = False

    async def handler(message, sink):
        nonlocal called
        called = True
        return RunResult()

    await SlackListener(handler).on_message_event(
        {"text": "hi there", "channel": "D1", "ts": "1", "channel_type": "im"},
        FakeSlackClient(),
    )
    assert called is False


async def test_a_dm_reaches_the_handler_once_enabled():
    seen = []

    async def handler(message, sink):
        seen.append(message.text)
        await sink.final("ok")
        return RunResult(output="ok")

    await dm_listener(handler).on_message_event(
        {"text": "hi there", "channel": "D1", "ts": "1", "channel_type": "im"},
        FakeSlackClient(),
    )
    assert seen == ["hi there"]


async def test_message_subtypes_and_bots_are_ignored():
    calls = []

    async def handler(message, sink):
        calls.append(message.text)
        return RunResult()

    listener = dm_listener(handler)
    client = FakeSlackClient()
    await listener.on_message_event(
        {"text": "edited", "channel": "D1", "ts": "1", "channel_type": "im",
         "subtype": "message_changed"}, client)
    await listener.on_message_event(
        {"text": "from a bot", "channel": "D1", "ts": "1", "channel_type": "im",
         "bot_id": "B1"}, client)
    assert calls == []


async def test_mention_from_another_bot_is_ignored_to_avoid_loops(caplog):
    calls = []

    async def handler(message, sink):
        calls.append(message.text)
        return RunResult()

    with caplog.at_level("INFO"):
        await SlackListener(handler).on_mention_event(
            {"text": "<@U0BOT> hi", "channel": "C1", "ts": "1", "bot_id": "B9"},
            FakeSlackClient(),
        )

    assert calls == []
    assert "Ignoring app_mention from bot" in caplog.text


async def test_mention_from_a_human_reaches_the_handler():
    seen = []

    async def handler(message, sink):
        seen.append(message.text)
        await sink.final("ok")
        return RunResult(output="ok")

    await SlackListener(handler).on_mention_event(
        {"text": "<@U0BOT> do the thing", "channel": "C1", "user": "U5", "ts": "1"},
        FakeSlackClient(),
    )
    assert seen == ["do the thing"]
