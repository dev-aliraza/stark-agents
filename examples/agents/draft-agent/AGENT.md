---
name: draft-agent
description: An unfinished agent. Used by example 02 to demonstrate exclude_agents.
provider: anthropic
base_url: ${STARK_BASE_URL}
api_key: ${STARK_API_KEY}
model: claude-opus-5
---

# Role

Work in progress — not ready to be part of the roster.

Example `02_custom_instructions.py` passes `exclude_agents=["draft-agent"]`, so this
directory is skipped during discovery even though its `AGENT.md` is perfectly valid.
