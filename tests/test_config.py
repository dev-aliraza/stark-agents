"""`stark.run(config=...)` — coercion, validation, and effect on the Slack listener."""

from __future__ import annotations

import pytest

from stark.config import (
    DEFAULT_DONE_EMOJI,
    DEFAULT_FAILED_EMOJI,
    DEFAULT_RUNNING_EMOJI,
    Config,
    ConfigError,
    SlackConfig,
)


# --- defaults -------------------------------------------------------------------------


def test_documented_defaults():
    slack = SlackConfig()
    assert slack.running_emoji == ":hourglass:"
    assert slack.done_emoji == ":white_check_mark:"
    assert slack.failed_emoji == ":x:"
    assert slack.starting_label == "Working on it"
    assert slack.update_interval == pytest.approx(1.2)


def test_module_constants_match_the_defaults():
    """The listener re-exports these, so they must not drift apart."""
    from stark.listeners import slack as slack_module

    assert slack_module.RUNNING_EMOJI == DEFAULT_RUNNING_EMOJI
    assert slack_module.DONE_EMOJI == DEFAULT_DONE_EMOJI
    assert slack_module.FAILED_EMOJI == DEFAULT_FAILED_EMOJI


def test_config_defaults_to_default_slack():
    assert Config().slack == SlackConfig()


# --- coercion -------------------------------------------------------------------------


def test_none_gives_defaults():
    assert Config.coerce(None).slack.running_emoji == ":hourglass:"


def test_nested_dict():
    config = Config.coerce({"slack": {"running_emoji": ":spinner:"}})
    assert config.slack.running_emoji == ":spinner:"
    # Unspecified keys keep their defaults.
    assert config.slack.done_emoji == ":white_check_mark:"


def test_config_instance_passes_through():
    original = Config(slack=SlackConfig(done_emoji=":done:"))
    assert Config.coerce(original) is original


def test_a_bare_slack_config_is_accepted():
    """An easy mistake to make, and unambiguous, so it is allowed."""
    config = Config.coerce(SlackConfig(failed_emoji=":boom:"))
    assert config.slack.failed_emoji == ":boom:"


def test_slack_config_instance_inside_a_dict():
    config = Config.coerce({"slack": SlackConfig(done_emoji=":ok:")})
    assert config.slack.done_emoji == ":ok:"


def test_all_three_icons_can_be_overridden():
    config = Config.coerce(
        {
            "slack": {
                "running_emoji": ":cyclone:",
                "done_emoji": ":heavy_check_mark:",
                "failed_emoji": ":no_entry_sign:",
            }
        }
    )
    assert config.slack.running_emoji == ":cyclone:"
    assert config.slack.done_emoji == ":heavy_check_mark:"
    assert config.slack.failed_emoji == ":no_entry_sign:"


def test_literal_unicode_emoji_is_allowed():
    config = Config.coerce({"slack": {"running_emoji": "⏳", "done_emoji": "✅"}})
    assert config.slack.running_emoji == "⏳"


# --- validation: a typo must not silently do nothing ---------------------------------


def test_unknown_slack_key_is_rejected():
    with pytest.raises(ConfigError, match="unknown config.slack key"):
        Config.coerce({"slack": {"runing_emoji": ":typo:"}})


def test_the_error_lists_the_valid_keys():
    with pytest.raises(ConfigError, match="running_emoji"):
        Config.coerce({"slack": {"nope": "x"}})


def test_unknown_top_level_key_is_rejected():
    with pytest.raises(ConfigError, match="unknown config key"):
        Config.coerce({"slak": {}})


def test_wrong_type_for_config_is_rejected():
    with pytest.raises(ConfigError, match="config must be a dict or Config"):
        Config.coerce("slack")


def test_wrong_type_for_slack_is_rejected():
    with pytest.raises(ConfigError, match="config.slack must be a dict"):
        Config.coerce({"slack": ":hourglass:"})


@pytest.mark.parametrize("value", ["", "   ", None, 42, [":x:"]])
def test_emoji_must_be_a_non_empty_string(value):
    with pytest.raises(ConfigError, match="must be a non-empty string"):
        SlackConfig(running_emoji=value)


def test_every_text_field_is_validated():
    for field in ("running_emoji", "done_emoji", "failed_emoji", "starting_label"):
        with pytest.raises(ConfigError, match=field):
            SlackConfig(**{field: ""})


def test_update_interval_must_be_a_number():
    with pytest.raises(ConfigError, match="must be a number"):
        SlackConfig(update_interval="fast")


def test_update_interval_cannot_be_negative():
    with pytest.raises(ConfigError, match="cannot be negative"):
        SlackConfig(update_interval=-1)


def test_update_interval_zero_is_allowed():
    assert SlackConfig(update_interval=0).update_interval == 0.0


def test_update_interval_accepts_an_int():
    assert SlackConfig(update_interval=3).update_interval == pytest.approx(3.0)


# --- effect on rendering -------------------------------------------------------------


pytest.importorskip("slack_bolt", reason="the slack listener needs the [slack] extra")


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

    @property
    def progress(self) -> str:
        return self.updates[-1]["text"] if self.updates else self.posted[0]["text"]


async def test_custom_icons_reach_the_rendered_progress():
    from stark.listeners.slack import SlackSink

    config = SlackConfig(
        running_emoji=":cyclone:",
        done_emoji=":heavy_check_mark:",
        failed_emoji=":no_entry_sign:",
        update_interval=0,
    )
    client = FakeSlackClient()
    sink = SlackSink(client, channel="C1", thread_ts="T1", config=config)

    await sink.event("agent_start", "worker: a task", key="c1")
    await sink.event("agent_start", "other: another", key="c2")
    await sink.event("agent_end", "worker done", key="c1")
    await sink.event("agent_error", "other exploded", key="c2")
    await sink.final("answer")

    progress = client.progress
    assert ":heavy_check_mark: ~worker: a task~" in progress
    assert ":no_entry_sign: ~other: another~" in progress

    # Everything settled, so the running icon is gone from the final render.
    assert ":cyclone:" not in progress
    # And the built-in defaults were genuinely replaced, not merely added alongside.
    assert ":hourglass:" not in progress
    assert ":white_check_mark:" not in progress
    assert ":x:" not in progress


async def test_default_icons_are_used_when_no_config_is_given():
    from stark.listeners.slack import SlackSink

    client = FakeSlackClient()
    sink = SlackSink(client, channel="C1", thread_ts="T1")

    await sink.event("agent_start", "worker: a task", key="c1")
    await sink.event("agent_end", "worker done", key="c1")
    await sink.final("answer")

    assert ":white_check_mark: ~worker: a task~" in client.progress


async def test_custom_starting_label_is_used():
    from stark.listeners.slack import SlackSink

    client = FakeSlackClient()
    sink = SlackSink(
        client, channel="C1", thread_ts="T1", config=SlackConfig(starting_label="Thinking")
    )

    await sink.status("working")

    assert client.progress == ":hourglass: Thinking"


async def test_custom_failed_icon_prefixes_an_error_reply():
    from stark.listeners.slack import SlackSink

    client = FakeSlackClient()
    sink = SlackSink(
        client, channel="C1", thread_ts="T1", config=SlackConfig(failed_emoji=":boom:")
    )

    await sink.error("provider exploded")

    assert client.posted[-1]["text"] == ":boom: provider exploded"


# --- the listener passes it down -----------------------------------------------------


async def test_build_listener_hands_the_config_to_slack(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-y")
    from stark.listeners import build_listener

    async def handler(message, sink):
        return None

    listener = build_listener(
        "slack", handler, config={"slack": {"running_emoji": ":spinner:"}}
    )
    assert listener.config.running_emoji == ":spinner:"


async def test_build_listener_defaults_when_no_config(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-y")
    from stark.listeners import build_listener

    async def handler(message, sink):
        return None

    listener = build_listener("slack", handler)
    assert listener.config.running_emoji == ":hourglass:"


async def test_run_async_rejects_a_bad_config_before_touching_the_agents_dir():
    """Validation happens before discovery, so a typo fails fast."""
    import stark

    with pytest.raises(ConfigError, match="unknown config.slack key"):
        await stark.run_async(
            agents="/nonexistent-path-that-would-otherwise-raise",
            config={"slack": {"runing_emoji": ":typo:"}},
        )
