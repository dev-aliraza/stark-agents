from __future__ import annotations

from ..errors import ListenerError
from .base import Handler, Listener, Message, ResponseSink

CLI = "cli"
SLACK = "slack"

SUPPORTED = (CLI, SLACK)


def validate_listener(kind: str) -> str:
    """Check a listener is usable before any expensive startup work happens.

    Catches an unknown name, a missing slack-bolt install, or absent Slack tokens
    up front, so we do not spin MCP servers up only to tear them straight down.
    """
    normalized = (kind or "").strip().lower()
    if normalized not in SUPPORTED:
        raise ListenerError(
            f"unknown listener '{kind}'; expected one of {', '.join(SUPPORTED)}"
        )

    if normalized == SLACK:
        from .slack import SlackListener

        SlackListener.preflight()

    return normalized


def build_listener(kind: str, handler: Handler, **options) -> Listener:
    """Instantiate a listener by name.

    Slack is imported lazily so the CLI path never needs slack-bolt installed.
    """
    normalized = (kind or "").strip().lower()

    if normalized == CLI:
        from .cli import CLIListener

        return CLIListener(handler, **options)

    if normalized == SLACK:
        from .slack import SlackListener

        return SlackListener(handler)

    raise ListenerError(
        f"unknown listener '{kind}'; expected one of {', '.join(SUPPORTED)}"
    )


__all__ = [
    "CLI",
    "SLACK",
    "SUPPORTED",
    "Handler",
    "Listener",
    "Message",
    "ResponseSink",
    "build_listener",
    "validate_listener",
]
