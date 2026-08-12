"""Slack dispatch and sink behaviour, exercised with a fake Slack client."""

from __future__ import annotations

import pytest

from stark.errors import ListenerError
from stark.listeners import validate_listener
from stark.listeners.slack import SlackListener, SlackSink
from stark.types import RunResult

pytestmark = pytest.mark.asyncio

pytest.importorskip("slack_bolt", reason="the slack listener needs the [slack] extra")


class FakeSlackClient:
    """Records chat.postMessage / chat.update calls instead of hitting Slack."""

    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ts": "1700000000.0001"}

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}


async def test_sink_posts_placeholder_then_edits_it():
    client = FakeSlackClient()
    sink = SlackSink(client, channel="C1", thread_ts="T1")

    await sink.status("working")
    assert client.posts[0]["channel"] == "C1"
    assert client.posts[0]["thread_ts"] == "T1"

    # Chunks accumulate; the first edit lands, the immediate next is throttled.
    await sink.chunk("Hello ")
    await sink.chunk("world")
    await sink.final("Hello world")

    assert client.updates[-1]["text"] == "Hello world"
    assert client.updates[-1]["ts"] == "1700000000.0001"


async def test_sink_posts_when_no_placeholder_exists():
    client = FakeSlackClient()
    sink = SlackSink(client, channel="C1", thread_ts=None)

    await sink.final("answer")

    assert client.posts[0]["text"] == "answer"
    assert client.posts[0]["thread_ts"] is None


async def test_sink_reports_errors_visibly():
    client = FakeSlackClient()
    sink = SlackSink(client, channel="C1", thread_ts="T1")
    await sink.status("working")
    await sink.error("provider exploded")

    assert ":warning: provider exploded" in client.updates[-1]["text"]


async def test_failed_edit_does_not_abort_the_run(caplog):
    class BrokenClient(FakeSlackClient):
        async def chat_update(self, **kwargs):
            raise RuntimeError("rate limited")

    client = BrokenClient()
    sink = SlackSink(client, channel="C1", thread_ts="T1")
    await sink.status("working")

    with caplog.at_level("WARNING"):
        await sink.final("answer")  # must not raise

    assert "chat_update failed" in caplog.text


async def test_dispatch_strips_the_mention_and_threads_the_reply():
    seen: list = []

    async def handler(message, sink):
        seen.append(message)
        await sink.final("done")
        return RunResult(output="done")

    listener = SlackListener(handler)
    client = FakeSlackClient()

    await listener._dispatch(
        {
            "text": "<@U0BOT> research checkout latency",
            "channel": "C123",
            "user": "U999",
            "ts": "1700000000.0002",
        },
        client,
    )

    assert seen[0].text == "research checkout latency"
    assert seen[0].channel == "C123"
    assert seen[0].user == "U999"
    assert seen[0].thread == "1700000000.0002"


async def test_dispatch_ignores_empty_text():
    called = False

    async def handler(message, sink):
        nonlocal called
        called = True
        return RunResult()

    await SlackListener(handler)._dispatch(
        {"text": "<@U0BOT>", "channel": "C1", "ts": "1"}, FakeSlackClient()
    )
    assert called is False


async def test_dispatch_reports_handler_errors_to_slack(caplog):
    async def handler(message, sink):
        raise RuntimeError("boom")

    client = FakeSlackClient()
    with caplog.at_level("ERROR"):
        await SlackListener(handler)._dispatch(
            {"text": "hi", "channel": "C1", "ts": "1"}, client
        )

    assert ":warning:" in client.updates[-1]["text"]
    assert "boom" in client.updates[-1]["text"]


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
