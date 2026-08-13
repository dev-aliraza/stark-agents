"""`config.slack.events`: which Slack events reach the handler, and which messages do.

Two layers are being tested. The config parses and validates the event map at startup, so a
typo or a malformed filter cannot become a silent bot. The listener then subscribes only to
what was asked for and applies each event's filter to the same text the handler would see.
"""

from __future__ import annotations

import pytest

from stark.config import (
    CHANNEL_TYPE_EVENTS,
    DEFAULT_SLACK_EVENTS,
    EVENT_SCOPES,
    SLACK_EVENTS,
    Config,
    ConfigError,
    SlackConfig,
)
from stark.types import RunResult

pytest.importorskip("slack_bolt", reason="the slack listener needs the [slack] extra")

from stark.listeners.slack import SlackListener  # noqa: E402


class FakeSlackClient:
    def __init__(self):
        self.posted: list[dict] = []
        self.updates: list[dict] = []

    async def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ts": "1.1"}

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}


def recorder():
    """A handler plus the list of texts it saw."""
    seen: list[str] = []

    async def handler(message, sink):
        seen.append(message.text)
        await sink.final("ok")
        return RunResult(output="ok")

    return handler, seen


def listener(events=None, **extra):
    handler, seen = recorder()
    config = SlackConfig(events=events, **extra)
    built = SlackListener(handler, config)
    built.bot_user_id = "U0BOT"
    return built, seen


def message_event(text: str, channel_type: str = "im", **extra) -> dict:
    return {
        "text": text,
        "channel": "C1",
        "user": "U9",
        "ts": "1.0",
        "channel_type": channel_type,
        **extra,
    }


# --- the vocabulary itself -----------------------------------------------------------


def test_every_event_has_a_scope():
    assert set(EVENT_SCOPES) == set(SLACK_EVENTS)


def test_every_message_flavour_maps_back_to_an_event():
    """channel_type is how a `message` event says which of the four it is."""
    assert set(CHANNEL_TYPE_EVENTS.values()) == {
        name for name in SLACK_EVENTS if name.startswith("message.")
    }


def test_the_default_is_mentions_only():
    assert DEFAULT_SLACK_EVENTS == ("app_mention",)
    assert SlackConfig().enabled_events == ("app_mention",)
    assert Config().slack.enabled_events == ("app_mention",)


def test_the_default_needs_no_history_scope():
    assert SlackConfig().required_scopes == ("chat:write", "app_mentions:read")


# --- parsing the event map -----------------------------------------------------------


def test_true_means_listen_unconditionally():
    config = SlackConfig(events={"app_mention": True})
    assert config.listens_to("app_mention")
    assert config.filter_for("app_mention") is None


def test_a_string_is_parsed_as_a_filter():
    config = SlackConfig(events={"message.channels": 'text.contains("=====")'})
    rule = config.filter_for("message.channels")
    assert rule is not None
    assert rule.matches({"text": "===== outage ====="}) is True
    assert rule.matches({"text": "ordinary"}) is False


def test_false_parks_an_event_without_deleting_the_line():
    config = SlackConfig(events={"app_mention": True, "message.im": False})
    assert config.enabled_events == ("app_mention",)


def test_a_plain_list_is_accepted_as_the_unfiltered_case():
    config = SlackConfig(events=["app_mention", "message.im"])
    assert config.enabled_events == ("app_mention", "message.im")
    assert all(config.filter_for(name) is None for name in config.enabled_events)


def test_events_are_ordered_canonically_whatever_the_input_order():
    """So logs, errors and scope lists are stable rather than dict-order dependent."""
    config = SlackConfig(events={"message.mpim": True, "app_mention": True, "message.im": True})
    assert config.enabled_events == ("app_mention", "message.im", "message.mpim")


def test_scopes_follow_the_enabled_events():
    config = SlackConfig(events=["app_mention", "message.channels", "message.groups"])
    assert config.required_scopes == (
        "chat:write",
        "app_mentions:read",
        "channels:history",
        "groups:history",
    )


def test_message_events_are_reported_separately():
    """They share one Bolt handler, so the listener needs to know if any are on."""
    assert SlackConfig(events=["app_mention"]).message_events == ()
    assert SlackConfig(events=["app_mention", "message.im"]).message_events == ("message.im",)


def test_describe_events_names_the_filters():
    config = SlackConfig(
        events={"app_mention": True, "message.channels": 'text.contains("go")'}
    )
    described = config.describe_events()
    assert "app_mention" in described
    assert 'message.channels when text.contains("go")' in described


# --- validation: a typo must not become a silent bot ---------------------------------


def test_an_unknown_event_name_is_rejected():
    with pytest.raises(ConfigError, match="unknown config.slack.events key"):
        SlackConfig(events={"message.channel": True})


def test_the_error_lists_the_valid_events():
    with pytest.raises(ConfigError, match="message.channels"):
        SlackConfig(events={"nope": True})


def test_a_malformed_filter_is_rejected_at_startup():
    with pytest.raises(ConfigError, match="not a valid filter"):
        SlackConfig(events={"app_mention": 'text.contains("unclosed'})


def test_an_unknown_field_in_a_filter_is_rejected():
    with pytest.raises(ConfigError, match="not a valid filter"):
        SlackConfig(events={"app_mention": 'subject.contains("x")'})


def test_an_empty_filter_string_is_rejected():
    """Ambiguous between "no filter" and a mistake, so it has to be spelled True."""
    with pytest.raises(ConfigError, match="empty string"):
        SlackConfig(events={"app_mention": "   "})


def test_a_non_string_non_bool_value_is_rejected():
    with pytest.raises(ConfigError, match="must be True, False, or a triggerRule"):
        SlackConfig(events={"app_mention": 42})


def test_enabling_nothing_is_rejected():
    with pytest.raises(ConfigError, match="enables no events"):
        SlackConfig(events={})


def test_disabling_everything_is_rejected():
    with pytest.raises(ConfigError, match="enables no events"):
        SlackConfig(events={"app_mention": False, "message.im": False})


def test_the_wrong_type_for_events_is_rejected():
    with pytest.raises(ConfigError, match="config.slack.events must be a dict"):
        SlackConfig(events="app_mention")


def test_allow_bots_must_be_a_boolean():
    with pytest.raises(ConfigError, match="allow_bots must be a boolean"):
        SlackConfig(allow_bots="yes")


def test_events_reach_slack_config_through_run_config():
    config = Config.coerce({"slack": {"events": {"message.im": True}}})
    assert config.slack.enabled_events == ("message.im",)


async def test_run_async_rejects_a_bad_event_before_touching_the_agents_dir():
    import stark

    with pytest.raises(ConfigError, match="unknown config.slack.events key"):
        await stark.run_async(
            agents="/nonexistent-path-that-would-otherwise-raise",
            config={"slack": {"events": {"message.dm": True}}},
        )


# --- the listener honours it ---------------------------------------------------------


async def test_only_the_enabled_message_flavour_is_handled():
    built, seen = listener(["message.im"])
    client = FakeSlackClient()

    await built.on_message_event(message_event("a dm", "im"), client)
    await built.on_message_event(message_event("a channel post", "channel"), client)
    await built.on_message_event(message_event("a private post", "group"), client)

    assert seen == ["a dm"]


async def test_a_channel_message_is_handled_when_enabled():
    built, seen = listener(["message.channels"])

    await built.on_message_event(message_event("hello channel", "channel"), FakeSlackClient())

    assert seen == ["hello channel"]


async def test_an_unrecognised_channel_type_is_ignored():
    built, seen = listener(["message.channels"])

    await built.on_message_event(message_event("what", "carrier_pigeon"), FakeSlackClient())

    assert seen == []


async def test_a_filter_keeps_only_matching_messages():
    built, seen = listener({"message.channels": 'text.contains("=====")'})
    client = FakeSlackClient()

    await built.on_message_event(message_event("===== outage =====", "channel"), client)
    await built.on_message_event(message_event("what is for lunch", "channel"), client)

    assert seen == ["===== outage ====="]


async def test_a_filtered_out_message_posts_absolutely_nothing():
    """Not even a progress message — this is the difference from a script triggerRule."""
    built, seen = listener({"message.channels": 'text.contains("=====")'})
    client = FakeSlackClient()

    await built.on_message_event(message_event("nothing to see", "channel"), client)

    assert seen == []
    assert client.posted == []
    assert client.updates == []


async def test_the_filter_sees_the_text_with_the_mention_stripped():
    """So a rule matches what the handler receives, not the raw Slack payload."""
    built, seen = listener({"app_mention": 'text.contains("deploy")'})

    await built.on_mention_event(
        {"text": "<@U0BOT> deploy the thing", "channel": "C1", "user": "U9", "ts": "1"},
        FakeSlackClient(),
    )

    assert seen == ["deploy the thing"]


async def test_a_filter_can_match_on_the_channel():
    built, seen = listener({"message.channels": 'channel.contains("C1")'})
    client = FakeSlackClient()

    await built.on_message_event(message_event("in scope", "channel"), client)
    await built.on_message_event(
        {**message_event("out of scope", "channel"), "channel": "C999"}, client
    )

    assert seen == ["in scope"]


async def test_each_event_gets_its_own_filter():
    built, seen = listener(
        {"app_mention": True, "message.channels": 'text.contains("=====")'}
    )
    client = FakeSlackClient()

    # A mention needs no marker.
    await built.on_mention_event(
        {"text": "<@U0BOT> anything at all", "channel": "C1", "user": "U9", "ts": "1"}, client
    )
    # A channel post does.
    await built.on_message_event(message_event("chatter", "channel"), client)
    await built.on_message_event(message_event("===== outage =====", "channel"), client)

    assert seen == ["anything at all", "===== outage ====="]


async def test_the_event_name_is_recorded_on_the_message():
    built, seen = listener(["message.channels"])
    captured: list = []

    async def handler(message, sink):
        captured.append(message.meta["slack_event"])
        await sink.final("ok")
        return RunResult()

    built.handler = handler
    await built.on_message_event(message_event("hi", "channel"), FakeSlackClient())

    assert captured == ["message.channels"]


# --- registration --------------------------------------------------------------------


def registered_events(config) -> set[str]:
    """Which Bolt event names a listener subscribes to for this config."""
    handler, _ = recorder()
    built = SlackListener(handler, config)
    names: set[str] = set()

    class FakeApp:
        def middleware(self, func):
            return func

        def event(self, name):
            names.add(name)
            return lambda func: func

    built._imports = staticmethod(lambda: (lambda token: FakeApp(), lambda app, t: None))
    built._build()
    return names


def test_only_the_mention_handler_is_registered_by_default(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-y")

    assert registered_events(SlackConfig()) == {"app_mention"}


def test_the_message_handler_is_registered_only_when_a_message_event_is_enabled(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-y")

    assert registered_events(SlackConfig(events=["message.im"])) == {"message"}
    assert registered_events(
        SlackConfig(events=["app_mention", "message.channels"])
    ) == {"app_mention", "message"}


# --- bots, ourselves, and duplicate mentions -----------------------------------------


async def test_our_own_message_is_always_ignored():
    """Answering ourselves is an unbounded loop, whatever allow_bots says."""
    built, seen = listener(["message.channels"], allow_bots=True)

    await built.on_message_event(
        {**message_event("my own words", "channel"), "user": "U0BOT"}, FakeSlackClient()
    )

    assert seen == []


async def test_a_bot_post_is_ignored_by_default(caplog):
    built, seen = listener(["message.channels"])

    with caplog.at_level("INFO"):
        await built.on_message_event(
            {**message_event("alert fired", "channel"), "bot_id": "B1",
             "subtype": "bot_message"},
            FakeSlackClient(),
        )

    assert seen == []
    assert "allow_bots" in caplog.text


async def test_a_bot_post_is_handled_when_allow_bots_is_on():
    """An alerting integration posting to a channel is a real trigger source."""
    built, seen = listener(
        {"message.channels": 'text.contains("=====")'}, allow_bots=True
    )

    await built.on_message_event(
        {**message_event("===== ArgoCD down =====", "channel"), "bot_id": "B1",
         "subtype": "bot_message"},
        FakeSlackClient(),
    )

    assert seen == ["===== ArgoCD down ====="]


async def test_allow_bots_does_not_open_up_other_subtypes():
    built, seen = listener(["message.channels"], allow_bots=True)

    await built.on_message_event(
        {**message_event("edited", "channel"), "subtype": "message_changed"},
        FakeSlackClient(),
    )

    assert seen == []


async def test_a_bot_mention_is_ignored_by_default():
    built, seen = listener(["app_mention"])

    await built.on_mention_event(
        {"text": "<@U0BOT> hi", "channel": "C1", "ts": "1", "bot_id": "B9"},
        FakeSlackClient(),
    )

    assert seen == []


async def test_a_mention_is_not_answered_twice_when_channels_are_also_enabled():
    """Slack sends both app_mention and message.channels for the same post."""
    built, seen = listener(["app_mention", "message.channels"])
    client = FakeSlackClient()
    event = {"text": "<@U0BOT> do it", "channel": "C1", "user": "U9", "ts": "1"}

    await built.on_mention_event(event, client)
    await built.on_message_event({**event, "channel_type": "channel"}, client)

    assert seen == ["do it"]


async def test_a_plain_channel_message_still_gets_through_alongside_mentions():
    built, seen = listener(["app_mention", "message.channels"])

    await built.on_message_event(message_event("no mention here", "channel"), FakeSlackClient())

    assert seen == ["no mention here"]


async def test_a_mention_is_handled_as_a_channel_message_when_app_mention_is_off():
    """Without app_mention there is no duplicate to guard against."""
    built, seen = listener(["message.channels"])

    await built.on_message_event(
        {**message_event("<@U0BOT> do it", "channel")}, FakeSlackClient()
    )

    assert seen == ["do it"]


async def test_dedup_is_skipped_before_the_bot_id_is_known():
    """auth.test has not run yet, so we cannot tell a self-mention from any other."""
    handler, seen = recorder()
    built = SlackListener(handler, SlackConfig(events=["app_mention", "message.channels"]))
    assert built.bot_user_id is None

    await built.on_message_event(
        {**message_event("<@U0BOT> do it", "channel")}, FakeSlackClient()
    )

    assert seen == ["do it"]
