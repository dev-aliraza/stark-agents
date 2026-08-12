from pathlib import Path

import pytest

from stark.errors import AgentDiscoveryError, AgentValidationError
from stark.parsers import discover_agents, parse_agent_file

VALID = """\
---
name: research-agent
description: Researches things.
provider: anthropic
model: claude-opus-5
---

Do the research.
"""


def write_agent(root: Path, directory: str, body: str = VALID) -> Path:
    path = root / directory
    path.mkdir(parents=True, exist_ok=True)
    (path / "AGENT.md").write_text(body, encoding="utf-8")
    return path


def test_missing_agents_dir_is_fatal(tmp_path):
    with pytest.raises(AgentDiscoveryError):
        discover_agents(tmp_path / "nope")


def test_loads_valid_agent(tmp_path):
    write_agent(tmp_path, "research-agent")
    agents = discover_agents(tmp_path)

    assert len(agents) == 1
    agent = agents[0]
    assert agent.name == "research-agent"
    assert agent.provider == "anthropic"
    assert agent.instructions == "Do the research."
    assert agent.tool_name == "agent__research-agent"
    # Optional metadata falls back to documented defaults.
    assert (agent.effort, agent.max_iterations, agent.max_output_tokens) == ("medium", 100, 4096)
    assert agent.mcp == []
    assert agent.enabled_mcp_servers == []


def test_directory_without_agent_md_is_skipped(tmp_path):
    (tmp_path / "not-an-agent").mkdir()
    (tmp_path / "not-an-agent" / "README.md").write_text("hi", encoding="utf-8")
    write_agent(tmp_path, "real-agent")

    assert [agent.name for agent in discover_agents(tmp_path)] == ["research-agent"]


def test_nested_agent_md_is_not_discovered(tmp_path):
    nested = tmp_path / "outer" / "inner"
    nested.mkdir(parents=True)
    (nested / "AGENT.md").write_text(VALID, encoding="utf-8")

    assert discover_agents(tmp_path) == []


def test_exclude_agents_skips_directory(tmp_path):
    write_agent(tmp_path, "research-agent")
    write_agent(tmp_path, "other-agent", VALID.replace("research-agent", "other-agent"))

    agents = discover_agents(tmp_path, exclude_agents=["other-agent"])
    assert [agent.name for agent in agents] == ["research-agent"]


def test_missing_mandatory_key_warns_and_skips(tmp_path, caplog):
    write_agent(tmp_path, "broken", VALID.replace("provider: anthropic\n", ""))
    write_agent(tmp_path, "good", VALID.replace("research-agent", "good-agent"))

    with caplog.at_level("WARNING"):
        agents = discover_agents(tmp_path)

    assert [agent.name for agent in agents] == ["good-agent"]
    assert "provider" in caplog.text


def test_duplicate_names_keep_the_first(tmp_path, caplog):
    write_agent(tmp_path, "a-copy")
    write_agent(tmp_path, "b-copy")

    with caplog.at_level("WARNING"):
        agents = discover_agents(tmp_path)

    assert len(agents) == 1
    assert "already used by" in caplog.text


def test_no_frontmatter_is_rejected(tmp_path):
    path = write_agent(tmp_path, "bare", "Just a body, no metadata.")
    with pytest.raises(AgentValidationError):
        parse_agent_file(path / "AGENT.md")


def test_optional_metadata_and_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "secret-value")
    body = """\
---
name: tuned
description: Tuned agent.
provider: openai
model: gpt-4o
effort: high
max_iterations: 7
max_output_tokens: 512
base_url: https://proxy.internal/v1
api_key: ${MY_KEY}
---

Body.
"""
    path = write_agent(tmp_path, "tuned", body)
    agent = parse_agent_file(path / "AGENT.md")

    assert agent.effort == "high"
    assert agent.max_iterations == 7
    assert agent.max_output_tokens == 512
    assert agent.base_url == "https://proxy.internal/v1"
    assert agent.api_key == "secret-value"


def test_unknown_effort_falls_back(tmp_path, caplog):
    path = write_agent(tmp_path, "odd", VALID.replace("---\n\nDo", "effort: turbo\n---\n\nDo"))
    with caplog.at_level("WARNING"):
        agent = parse_agent_file(path / "AGENT.md")

    assert agent.effort == "medium"
    assert "turbo" in caplog.text
