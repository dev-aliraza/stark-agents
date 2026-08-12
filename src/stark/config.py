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

# Slack progress-message defaults.
DEFAULT_RUNNING_EMOJI = ":hourglass:"
DEFAULT_DONE_EMOJI = ":white_check_mark:"
DEFAULT_FAILED_EMOJI = ":x:"
DEFAULT_STARTING_LABEL = "Working on it"
# Seconds between chat.update calls, to stay clear of Slack's ~1/second edit limit.
DEFAULT_UPDATE_INTERVAL = 1.2


class ConfigError(StarkError):
    """A `config=` value is not usable."""


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"config.slack.{field_name} must be a non-empty string, got {value!r}"
        )
    return value


@dataclass
class SlackConfig:
    """How the Slack listener renders its progress message.

    Emoji may be a Slack shortcode (`:hourglass:`) or a literal character (`⏳`). A
    shortcode that is not in the workspace renders as the literal `:name:` text, so a
    custom emoji has to be added there first.
    """

    running_emoji: str = DEFAULT_RUNNING_EMOJI
    done_emoji: str = DEFAULT_DONE_EMOJI
    failed_emoji: str = DEFAULT_FAILED_EMOJI
    starting_label: str = DEFAULT_STARTING_LABEL
    update_interval: float = DEFAULT_UPDATE_INTERVAL

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
