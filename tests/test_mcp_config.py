"""Parsing of the `mcp:` list in AGENT.md frontmatter."""

from pathlib import Path

from stark.parsers import parse_agent_file

HEADER = """\
---
name: mcp-agent
description: Uses MCP.
provider: anthropic
model: claude-opus-5
"""


def write(tmp_path: Path, mcp_block: str) -> Path:
    directory = tmp_path / "mcp-agent"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "AGENT.md").write_text(f"{HEADER}{mcp_block}---\n\nBody.\n", encoding="utf-8")
    return directory / "AGENT.md"


def parse(tmp_path: Path, mcp_block: str):
    return parse_agent_file(write(tmp_path, mcp_block))


# --- the documented shape -------------------------------------------------------------


def test_omitted_mcp_key_means_no_servers(tmp_path):
    agent = parse(tmp_path, "")
    assert agent.mcp == []
    assert agent.enabled_mcp_servers == []


def test_single_stdio_server(tmp_path):
    agent = parse(
        tmp_path,
        """\
mcp:
  - name: slack
    enable: true
    command: uvx
    args: ["mcp-slack"]
    exclude: ["send_message"]
""",
    )

    assert len(agent.mcp) == 1
    server = agent.mcp[0]
    assert server.name == "slack"
    assert server.enable is True
    assert server.transport == "stdio"
    assert server.command == "uvx"
    assert server.args == ["mcp-slack"]
    assert server.exclude == ["send_message"]
    assert server.include == []
    assert agent.enabled_mcp_servers == [server]


def test_enable_defaults_to_true_when_omitted(tmp_path):
    """Listing a server is intent to use it; `enable: false` is how you park one."""
    agent = parse(
        tmp_path,
        """\
mcp:
  - name: slack
    command: uvx
    args: ["mcp-slack"]
""",
    )

    assert agent.mcp[0].enable is True
    assert [server.name for server in agent.enabled_mcp_servers] == ["slack"]


def test_disabled_servers_are_kept_but_not_started(tmp_path):
    agent = parse(
        tmp_path,
        """\
mcp:
  - name: slack
    enable: true
    command: uvx
    args: ["mcp-slack"]
  - name: jira
    enable: false
    command: uvx
    args: ["mcp-atlassian"]
""",
    )

    # Both parse, so a disabled entry stays visible for inspection...
    assert [server.name for server in agent.mcp] == ["slack", "jira"]
    # ...but only the enabled one is ever started.
    assert [server.name for server in agent.enabled_mcp_servers] == ["slack"]


def test_every_server_disabled_yields_nothing_to_start(tmp_path):
    agent = parse(
        tmp_path,
        """\
mcp:
  - name: slack
    enable: false
    command: uvx
    args: ["mcp-slack"]
""",
    )

    assert len(agent.mcp) == 1
    assert agent.enabled_mcp_servers == []


def test_multiple_servers_preserve_order(tmp_path):
    agent = parse(
        tmp_path,
        """\
mcp:
  - name: slack
    command: uvx
    args: ["mcp-slack"]
  - name: jira
    command: uvx
    args: ["mcp-atlassian"]
  - name: github
    command: uvx
    args: ["mcp-github"]
""",
    )

    assert [server.name for server in agent.mcp] == ["slack", "jira", "github"]


def test_env_and_headers_expand_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("SUPPLIER_TOKEN", raising=False)

    agent = parse(
        tmp_path,
        """\
mcp:
  - name: slack
    command: uvx
    args: ["mcp-slack"]
    env:
      SLACK_BOT_TOKEN: ${SLACK_BOT_TOKEN}
      REGION: ${MCP_REGION:-eu-west-1}
  - name: remote
    transport: streamable_http
    url: https://mcp.example.com/mcp
    headers:
      Authorization: Bearer ${SUPPLIER_TOKEN:-anonymous}
""",
    )

    slack, remote = agent.mcp
    assert slack.env == {"SLACK_BOT_TOKEN": "xoxb-test", "REGION": "eu-west-1"}
    assert remote.headers == {"Authorization": "Bearer anonymous"}


def test_streamable_http_server(tmp_path):
    agent = parse(
        tmp_path,
        """\
mcp:
  - name: remote
    enable: true
    transport: streamable_http
    url: https://mcp.example.com/mcp
    include: ["search", "fetch"]
""",
    )

    server = agent.mcp[0]
    assert server.transport == "streamable_http"
    assert server.url == "https://mcp.example.com/mcp"
    assert server.command is None
    assert server.include == ["search", "fetch"]


def test_include_and_exclude_default_to_empty(tmp_path):
    agent = parse(tmp_path, "mcp:\n  - name: slack\n    command: uvx\n")
    assert (agent.mcp[0].include, agent.mcp[0].exclude) == ([], [])


# --- malformed entries are dropped, never fatal ---------------------------------------


def test_entry_without_a_name_is_dropped(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        agent = parse(
            tmp_path,
            """\
mcp:
  - command: uvx
    args: ["mcp-slack"]
  - name: jira
    command: uvx
""",
        )

    assert [server.name for server in agent.mcp] == ["jira"]
    assert "needs a 'name'" in caplog.text


def test_stdio_entry_without_a_command_is_dropped(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        agent = parse(
            tmp_path,
            """\
mcp:
  - name: broken
    args: ["no-command"]
  - name: good
    command: uvx
""",
        )

    assert [server.name for server in agent.mcp] == ["good"]
    assert "needs a 'command'" in caplog.text


def test_http_entry_without_a_url_is_dropped(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        agent = parse(
            tmp_path,
            """\
mcp:
  - name: broken
    transport: streamable_http
""",
        )

    assert agent.mcp == []
    assert "needs a 'url'" in caplog.text


def test_unknown_transport_is_dropped(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        agent = parse(
            tmp_path,
            """\
mcp:
  - name: odd
    transport: websocket
    url: wss://example.com
""",
        )

    assert agent.mcp == []
    assert "unsupported transport 'websocket'" in caplog.text


def test_duplicate_names_keep_the_first(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        agent = parse(
            tmp_path,
            """\
mcp:
  - name: slack
    command: uvx
    args: ["first"]
  - name: slack
    command: uvx
    args: ["second"]
""",
        )

    assert len(agent.mcp) == 1
    assert agent.mcp[0].args == ["first"]
    assert "declared more than once" in caplog.text


def test_non_mapping_entry_is_dropped(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        agent = parse(tmp_path, 'mcp:\n  - "just a string"\n  - name: ok\n    command: uvx\n')

    assert [server.name for server in agent.mcp] == ["ok"]
    assert "must be a mapping" in caplog.text


def test_scalar_fields_tolerate_wrong_types(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        agent = parse(
            tmp_path,
            """\
mcp:
  - name: slack
    command: uvx
    args: "not-a-list"
    env: "not-a-mapping"
    exclude: "not-a-list"
""",
        )

    server = agent.mcp[0]
    assert (server.args, server.env, server.exclude) == ([], {}, [])
    assert "args must be a list" in caplog.text
    assert "env must be a mapping" in caplog.text


def test_the_old_nested_mapping_form_explains_the_new_shape(tmp_path, caplog):
    """0.3.0 used `mcp: {enable: ..., servers: {...}}`. Point people at the list form."""
    with caplog.at_level("WARNING"):
        agent = parse(
            tmp_path,
            """\
mcp:
  enable: true
  servers:
    slack:
      command: uvx
""",
        )

    assert agent.mcp == []
    assert "must be a list of servers" in caplog.text
    assert "- name: slack" in caplog.text


def test_mcp_scalar_is_ignored(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        agent = parse(tmp_path, "mcp: true\n")

    assert agent.mcp == []
    assert "must be a list of servers" in caplog.text
