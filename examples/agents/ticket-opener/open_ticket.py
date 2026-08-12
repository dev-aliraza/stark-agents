#!/usr/bin/env python3
"""A deterministic script agent: no LLM, no network.

Stands in for "create a Jira ticket" so the example runs with no credentials. In a real
deployment this is where you would call the Jira REST API, or shell out to a CLI.

`run()` is the only contract. It receives a plain dict — deliberately not a Stark object,
so this file imports nothing from stark and can be unit-tested on its own:

    {
      "text":           the message, mention stripped
      "user":           author id, or "cli"
      "channel":        channel id, or None outside Slack
      "thread":         thread ts, or None outside Slack
      "meta":           raw listener payload
      "agent":          this agent's name
      "workspace":      this agent's directory
      "prior_outputs":  [{"agent", "output", "error"}] from higher-priority bands
    }

Return a string (or anything JSON-serialisable) and it becomes this agent's output.
Raising is safe: the phase is fail-open, and the error is reported as a step and passed to
the orchestrator as context.

`async def run(message)` works too, if you need to await something.
"""

import hashlib
import re

MARKER = re.compile(r"=+")


def summarize(text: str) -> str:
    """First meaningful line, with the escalation markers stripped off."""
    for line in text.splitlines():
        cleaned = MARKER.sub("", line).strip()
        if cleaned:
            return cleaned[:120]
    return "Escalation with no description"


def reference(message: dict) -> str:
    """A stable id derived from the thread, so re-running yields the same ticket.

    A real integration would ask the tracker for the key it assigned. Deriving it from the
    thread is what makes this example idempotent: the same thread always maps to the same
    reference, which is the property you want when Slack redelivers an event.
    """
    seed = message.get("thread") or message.get("text") or ""
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:4].upper()
    return f"SUPPORT-{int(digest, 16) % 9000 + 1000}"


def run(message: dict) -> str:
    key = reference(message)
    title = summarize(message["text"])
    where = message.get("channel") or "cli"

    return (
        f":ticket: Opened *{key}* — {title}\n"
        f"_reported by {message.get('user') or 'unknown'} in {where}_"
    )
