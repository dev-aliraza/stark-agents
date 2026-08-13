#!/usr/bin/env python3
"""An `after_orchestrator` script agent: it acts on the answer, not on the question.

Stands in for "write this reply to an audit log" so the example runs with no credentials.
In a real deployment this is where you would insert a row, put an object in S3, or append
to a ticket.

The payload is the same dict every script agent receives, but two keys only carry anything
here:

    {
      "orchestrator_output":  the answer that was just delivered ("" if no model ran)
      "prior_outputs":        [{"agent", "output", "error"}] from everything earlier in
                              this query — the before-phase agents and anything the
                              orchestrator delegated to
      ...                     text, user, channel, thread, meta, agent, workspace,
                              invocation, task, context
    }

Whatever this returns cannot reach the model: the orchestrator's turn is over by the time
this runs. It goes to the user when `send_output: true`, and to the logs either way.
"""

import hashlib


def reference(message: dict) -> str:
    """A stable id, so re-archiving the same answer does not create a second record."""
    seed = f"{message.get('thread') or message.get('text') or ''}|{message['orchestrator_output']}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:6].upper()
    return f"AUDIT-{digest}"


def run(message: dict) -> str:
    answer = message["orchestrator_output"]
    if not answer.strip():
        # No llm agents, or the run failed before an answer existed. Nothing to file.
        return ""

    earlier = [item["agent"] for item in message["prior_outputs"]]
    trail = f", after {', '.join(earlier)}" if earlier else ""

    return (
        f":card_index_dividers: Archived as *{reference(message)}* — "
        f"{len(answer)} characters{trail}."
    )
