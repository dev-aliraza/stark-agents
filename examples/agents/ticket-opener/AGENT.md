---
name: ticket-opener
description: Opens a tracking ticket for messages that carry the ===== escalation marker.
type: script
script: open_ticket.py
priority: 200
send_output: true
triggerPoint: before_orchestrator
triggerRule: 'text.contains("=====")'
avoid_orchestrator: true
---

# Not an instruction file

A `type: script` agent has no model, so this body is never sent anywhere — it is
documentation for whoever maintains the agent.

The work happens in `open_ticket.py`, which exposes a `run(message)` function.

`triggerPoint: before_orchestrator` is what makes this agent run on its own — early enough
for the answer to mention the ticket. `triggerRule` then decides *which* messages it fires
for. Without the trigger point the rule would do nothing at all: the agent would sit in the
orchestrator's tool list and wait to be asked.

Because `send_output: true`, the returned text is posted straight to the user *and* handed
to the LLM orchestrator, which is told not to repeat it. With `send_output: false` it would
reach only the orchestrator.

`priority: 200` puts this agent ahead of the default band (100), so anything that needs the
ticket reference can read it from `prior_outputs`.

`avoid_orchestrator: true` keeps it out of the orchestrator's tool list, so the marker is the
only thing that can open a ticket — the model cannot decide to file one. Drop that line and
the agent becomes delegatable as well, which is the right default for a script whose action
is safe to take on request.
