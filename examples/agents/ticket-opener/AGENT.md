---
name: ticket-opener
description: Opens a tracking ticket for messages that carry the ===== escalation marker.
type: script
script: open_ticket.py
priority: 200
send_output: true
triggerRule: 'text.contains("=====")'
---

# Not an instruction file

A `type: script` agent has no model, so this body is never sent anywhere — it is
documentation for whoever maintains the agent.

The work happens in `open_ticket.py`, which exposes a `run(message)` function. Stark calls
it directly whenever `triggerRule` matches an inbound message.

Because `send_output: true`, the returned text is posted straight to the user *and* handed
to the LLM orchestrator, which is told not to repeat it. With `send_output: false` it would
reach only the orchestrator.

`priority: 200` puts this agent ahead of the default band (100), so anything that needs the
ticket reference can read it from `prior_outputs`.
