"""Developer-supplied configuration passed to `stark.run(config=...)`.

Distinct from AGENT.md metadata: that is authored by whoever writes an agent and is
parsed forgivingly, because one bad file should not stop the process. This is written in
Python by whoever embeds Stark, so a typo here raises immediately — silently ignoring an
unknown key would mean a customisation that never takes effect.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from .errors import StarkError
from .triggers import TriggerRule, TriggerRuleError
from .triggers import parse as parse_trigger_rule

# Slack progress-message defaults.
DEFAULT_RUNNING_EMOJI = ":hourglass:"
DEFAULT_DONE_EMOJI = ":white_check_mark:"
DEFAULT_FAILED_EMOJI = ":x:"
DEFAULT_STARTING_LABEL = "Working on it"
# Seconds between chat.update calls, to stay clear of Slack's ~1/second edit limit.
DEFAULT_UPDATE_INTERVAL = 1.2

# Slack events the listener can subscribe to, named exactly as Slack names them under
# Event Subscriptions — the same strings have to be ticked there, so a mismatch between
# the two is visible rather than mysterious.
EVENT_APP_MENTION = "app_mention"
EVENT_MESSAGE_IM = "message.im"
EVENT_MESSAGE_CHANNELS = "message.channels"
EVENT_MESSAGE_GROUPS = "message.groups"
EVENT_MESSAGE_MPIM = "message.mpim"

SLACK_EVENTS = (
    EVENT_APP_MENTION,
    EVENT_MESSAGE_IM,
    EVENT_MESSAGE_CHANNELS,
    EVENT_MESSAGE_GROUPS,
    EVENT_MESSAGE_MPIM,
)

# Only mentions by default. A bot that answers solely when named cannot be surprised, and
# widening the net is a decision worth making on purpose.
DEFAULT_SLACK_EVENTS = (EVENT_APP_MENTION,)

# A `message` event carries its flavour in `channel_type`; this maps that back to the
# event name a developer configured.
CHANNEL_TYPE_EVENTS = {
    "im": EVENT_MESSAGE_IM,
    "channel": EVENT_MESSAGE_CHANNELS,
    "group": EVENT_MESSAGE_GROUPS,
    "mpim": EVENT_MESSAGE_MPIM,
}

# Posting a reply always needs chat:write; reading each event needs its own scope.
BASE_SCOPES = ("chat:write",)
EVENT_SCOPES = {
    EVENT_APP_MENTION: "app_mentions:read",
    EVENT_MESSAGE_IM: "im:history",
    EVENT_MESSAGE_CHANNELS: "channels:history",
    EVENT_MESSAGE_GROUPS: "groups:history",
    EVENT_MESSAGE_MPIM: "mpim:history",
}


class ConfigError(StarkError):
    """A `config=` value is not usable."""


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"config.slack.{field_name} must be a non-empty string, got {value!r}"
        )
    return value


def _parse_events(raw: Any) -> dict[str, TriggerRule | None]:
    """Normalise `events` into `{event name: filter or None}` for the enabled events.

    Accepts a mapping of event name to `True` (listen unconditionally), a triggerRule
    expression (listen when it matches), or `False` (do not subscribe). A plain list or
    tuple of names is accepted as the unconditional case, since that reads naturally when
    no filtering is wanted.
    """
    if raw is None:
        raw = {name: True for name in DEFAULT_SLACK_EVENTS}
    if isinstance(raw, (list, tuple, set)):
        raw = {name: True for name in raw}
    if not isinstance(raw, dict):
        raise ConfigError(
            "config.slack.events must be a dict of event name to True/False or a "
            f"triggerRule expression, got {type(raw).__name__}"
        )

    unknown = sorted(str(name) for name in raw if name not in SLACK_EVENTS)
    if unknown:
        raise ConfigError(
            f"unknown config.slack.events key(s) {', '.join(unknown)} — expected "
            f"{', '.join(SLACK_EVENTS)}"
        )

    events: dict[str, TriggerRule | None] = {}
    for name in SLACK_EVENTS:  # canonical order, so logs and errors are stable
        if name not in raw:
            continue
        value = raw[name]
        # `False` and `None` are how you park an event without deleting the line, matching
        # `enable: false` on an MCP server.
        if value is False or value is None:
            continue
        events[name] = _parse_event_filter(name, value)

    # An empty set subscribes to nothing, so the bot could never answer anything. That is
    # always a mistake, and silence is the hardest failure to diagnose.
    if not events:
        raise ConfigError(
            "config.slack.events enables no events, so the listener would never respond. "
            f"Enable at least one of {', '.join(SLACK_EVENTS)}."
        )
    return events


def _parse_event_filter(name: str, value: Any) -> TriggerRule | None:
    """One `events` value: None for no filter, or the parsed expression."""
    if value is True:
        return None
    if isinstance(value, TriggerRule):
        return value
    if isinstance(value, str):
        if not value.strip():
            raise ConfigError(
                f"config.slack.events['{name}'] is an empty string; use True to listen "
                "for every one of these events"
            )
        try:
            return parse_trigger_rule(value)
        except TriggerRuleError as exc:
            raise ConfigError(
                f"config.slack.events['{name}'] is not a valid filter — {exc}"
            ) from exc
    raise ConfigError(
        f"config.slack.events['{name}'] must be True, False, or a triggerRule "
        f"expression string, got {value!r}"
    )


@dataclass
class SlackConfig:
    """What the Slack listener answers, and how it renders its progress message.

    Emoji may be a Slack shortcode (`:hourglass:`) or a literal character (`⏳`). A
    shortcode that is not in the workspace renders as the literal `:name:` text, so a
    custom emoji has to be added there first.

    `events` decides what the bot even sees. Omit it and only `app_mention` is handled —
    the bot answers when named and is otherwise silent. Each event may carry a filter, in
    the same expression language as an agent's `triggerRule`:

        events={
            "app_mention": True,                            # always
            "message.im": True,
            "message.channels": 'text.contains("=====")',    # only these
        }

    A filter that does not match means no reply at all — not even a progress message. That
    is the difference between this and a script agent's `triggerRule`: this decides whether
    to respond, that decides which deterministic agent runs once we have.
    """

    running_emoji: str = DEFAULT_RUNNING_EMOJI
    done_emoji: str = DEFAULT_DONE_EMOJI
    failed_emoji: str = DEFAULT_FAILED_EMOJI
    starting_label: str = DEFAULT_STARTING_LABEL
    update_interval: float = DEFAULT_UPDATE_INTERVAL
    events: Any = None
    allow_bots: bool = False

    def __post_init__(self) -> None:
        for name in ("running_emoji", "done_emoji", "failed_emoji", "starting_label"):
            setattr(self, name, _require_text(getattr(self, name), name))

        if not isinstance(self.update_interval, (int, float)) or isinstance(
            self.update_interval, bool
        ):
            raise ConfigError(
                f"config.slack.update_interval must be a number, got {self.update_interval!r}"
            )
        if self.update_interval < 0:
            raise ConfigError("config.slack.update_interval cannot be negative")
        self.update_interval = float(self.update_interval)

        if not isinstance(self.allow_bots, bool):
            raise ConfigError(
                f"config.slack.allow_bots must be a boolean, got {self.allow_bots!r}"
            )

        # Parsed here rather than on first use, so a malformed filter fails at startup
        # instead of the first message that would have matched it.
        self.events = _parse_events(self.events)

    # --- what the listener asks of it -------------------------------------------------

    @property
    def enabled_events(self) -> tuple[str, ...]:
        return tuple(self.events)

    def listens_to(self, event: str) -> bool:
        return event in self.events

    def filter_for(self, event: str) -> TriggerRule | None:
        return self.events.get(event)

    @property
    def message_events(self) -> tuple[str, ...]:
        """The enabled `message.*` events, which all arrive through one Bolt handler."""
        return tuple(name for name in self.events if name.startswith("message."))

    @property
    def required_scopes(self) -> tuple[str, ...]:
        """Bot-token scopes this configuration actually needs.

        Derived rather than fixed, so the startup check names the scope missing for an
        event you asked for instead of one you did not.
        """
        scopes = list(BASE_SCOPES)
        scopes.extend(EVENT_SCOPES[name] for name in self.events)
        return tuple(dict.fromkeys(scopes))

    def describe_events(self) -> str:
        """A one-line summary for the startup log."""
        parts = []
        for name, rule in self.events.items():
            parts.append(name if rule is None else f"{name} when {rule}")
        return ", ".join(parts)

    @classmethod
    def coerce(cls, value: Any) -> "SlackConfig":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ConfigError(
                f"config.slack must be a dict or SlackConfig, got {type(value).__name__}"
            )

        known = {field.name for field in fields(cls)}
        unknown = sorted(set(value) - known)
        if unknown:
            raise ConfigError(
                f"unknown config.slack key(s) {', '.join(unknown)} — "
                f"expected {', '.join(sorted(known))}"
            )
        return cls(**value)


@dataclass
class Config:
    """Top-level configuration for a Stark process."""

    slack: SlackConfig = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.slack = SlackConfig.coerce(self.slack)

    @classmethod
    def coerce(cls, value: Any) -> "Config":
        """Accept None, a Config, or a plain nested dict."""
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, SlackConfig):
            # A bare SlackConfig is an easy mistake and unambiguous, so allow it.
            return cls(slack=value)
        if not isinstance(value, dict):
            raise ConfigError(
                f"config must be a dict or Config, got {type(value).__name__}"
            )

        known = {field.name for field in fields(cls)}
        unknown = sorted(set(value) - known)
        if unknown:
            raise ConfigError(
                f"unknown config key(s) {', '.join(unknown)} — "
                f"expected {', '.join(sorted(known))}"
            )
        return cls(**value)
