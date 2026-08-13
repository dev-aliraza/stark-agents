---
name: answer-archiver
description: Files the final answer against its thread so support replies stay auditable.
type: script
script: archive_answer.py
triggerPoint: after_orchestrator
avoid_orchestrator: true
send_output: true
---

# Not an instruction file

A `type: script` agent has no model, so this body is documentation for whoever maintains
the agent. The work happens in `archive_answer.py`.

`triggerPoint: after_orchestrator` is the point of this example. It gives the agent an
automatic run — without a `triggerPoint` a script agent never fires on its own — and puts
that run *after* the answer has been delivered, so `run()` receives it as
`message["orchestrator_output"]`. That key is empty for a `before_orchestrator` agent,
because there is no answer yet. Anything that acts *on* a reply rather than informing it
belongs here: archiving, auditing, notifying.

There is no `triggerRule`, so the automatic run fires on every message.

`avoid_orchestrator: true` withholds it from the orchestrator's tool list. Archiving is a
bookkeeping step that should happen exactly once per answer, not something a model decides
to invoke — and its output could not reach the model anyway, since that turn is already
finished by the time it runs.

Those two together are the "always, and only, automatically" shape: a trigger point with no
rule means every message, and hiding it from the orchestrator means nothing can run it a
second time.

`send_output: true` posts the audit line to the user. Drop it and the step is silent: it
still shows as a progress step, but only the person reading the logs sees the reference.
