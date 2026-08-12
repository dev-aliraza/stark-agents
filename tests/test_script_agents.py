"""Script agent metadata validation and the `run()` contract."""

from __future__ import annotations

import pytest

from stark.errors import AgentValidationError
from stark.orchestration.script_runner import ScriptLoadError, ScriptRunner, load_entry_point
from stark.parsers import discover_agents, parse_agent_file
from stark.types import AGENT_TYPE_LLM, AGENT_TYPE_SCRIPT, DEFAULT_PRIORITY


def write_agent(root, name: str, frontmatter: str, script: str | None = None) -> object:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    if script is not None:
        (directory / "handler.py").write_text(script, encoding="utf-8")
    (directory / "AGENT.md").write_text(
        f"---\nname: {name}\ndescription: A test agent.\n{frontmatter}---\n\nBody.\n",
        encoding="utf-8",
    )
    return directory / "AGENT.md"


SIMPLE = "def run(message):\n    return 'ok'\n"


# --- type dispatch --------------------------------------------------------------------


def test_type_defaults_to_llm(tmp_path):
    agent = parse_agent_file(
        write_agent(tmp_path, "a", "provider: anthropic\nmodel: claude-opus-5\n")
    )
    assert agent.type == AGENT_TYPE_LLM
    assert agent.is_llm and not agent.is_script


def test_script_agent_needs_no_provider_or_model(tmp_path):
    agent = parse_agent_file(
        write_agent(tmp_path, "a", "type: script\nscript: handler.py\n", SIMPLE)
    )
    assert agent.type == AGENT_TYPE_SCRIPT
    assert agent.is_script
    assert (agent.provider, agent.model) == ("", "")


def test_llm_agent_still_requires_provider_and_model(tmp_path):
    with pytest.raises(AgentValidationError, match="missing mandatory key.*model"):
        parse_agent_file(write_agent(tmp_path, "a", "provider: anthropic\n"))


def test_script_agent_requires_a_script(tmp_path):
    with pytest.raises(AgentValidationError, match="missing mandatory key.*script"):
        parse_agent_file(write_agent(tmp_path, "a", "type: script\n"))


def test_unknown_type_is_rejected(tmp_path):
    with pytest.raises(AgentValidationError, match="unknown type 'robot'"):
        parse_agent_file(write_agent(tmp_path, "a", "type: robot\n"))


# --- script file validation, all at load time ----------------------------------------


def test_missing_script_file_fails_at_load(tmp_path):
    with pytest.raises(AgentValidationError, match="does not exist"):
        parse_agent_file(write_agent(tmp_path, "a", "type: script\nscript: ghost.py\n", SIMPLE))


def test_script_outside_the_agent_directory_is_rejected(tmp_path):
    with pytest.raises(AgentValidationError, match="outside the agent directory"):
        parse_agent_file(
            write_agent(tmp_path, "a", "type: script\nscript: ../escape.py\n", SIMPLE)
        )


def test_script_path_resolves_inside_the_agent_dir(tmp_path):
    agent = parse_agent_file(
        write_agent(tmp_path, "a", "type: script\nscript: handler.py\n", SIMPLE)
    )
    assert agent.script_path.is_file()
    assert agent.script_path.parent == agent.path


# --- optional script metadata --------------------------------------------------------


def test_priority_defaults_to_100(tmp_path):
    agent = parse_agent_file(
        write_agent(tmp_path, "a", "type: script\nscript: handler.py\n", SIMPLE)
    )
    assert agent.priority == DEFAULT_PRIORITY
    assert agent.send_output is False
    assert agent.trigger_rule is None


def test_priority_and_send_output_are_read(tmp_path):
    agent = parse_agent_file(
        write_agent(
            tmp_path,
            "a",
            "type: script\nscript: handler.py\npriority: 300\nsend_output: true\n",
            SIMPLE,
        )
    )
    assert agent.priority == 300
    assert agent.send_output is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("yes", True), ("false", False), ("no", False), ("0", False)],
)
def test_send_output_coercion(tmp_path, raw, expected):
    agent = parse_agent_file(
        write_agent(
            tmp_path,
            f"a{raw}",
            f"type: script\nscript: handler.py\nsend_output: {raw}\n",
            SIMPLE,
        )
    )
    assert agent.send_output is expected


def test_no_trigger_rule_means_always_runs(tmp_path):
    agent = parse_agent_file(
        write_agent(tmp_path, "a", "type: script\nscript: handler.py\n", SIMPLE)
    )
    assert agent.triggered_by({"text": "anything at all"}) is True
    assert agent.triggered_by({}) is True


def test_trigger_rule_is_parsed_at_load(tmp_path):
    agent = parse_agent_file(
        write_agent(
            tmp_path,
            "a",
            'type: script\nscript: handler.py\ntriggerRule: \'text.contains("=====")\'\n',
            SIMPLE,
        )
    )
    assert agent.trigger_rule is not None
    assert agent.triggered_by({"text": "===== x ====="}) is True
    assert agent.triggered_by({"text": "nope"}) is False


def test_malformed_trigger_rule_fails_the_agent_at_load(tmp_path):
    with pytest.raises(AgentValidationError, match="invalid triggerRule"):
        parse_agent_file(
            write_agent(
                tmp_path,
                "a",
                'type: script\nscript: handler.py\ntriggerRule: \'text.contains("A"\'\n',
                SIMPLE,
            )
        )


def test_one_bad_agent_does_not_stop_discovery(tmp_path, caplog):
    write_agent(tmp_path, "broken", "type: script\nscript: ghost.py\n", SIMPLE)
    write_agent(tmp_path, "good", "type: script\nscript: handler.py\n", SIMPLE)

    with caplog.at_level("WARNING"):
        agents = discover_agents(tmp_path)

    assert [agent.name for agent in agents] == ["good"]
    assert "does not exist" in caplog.text


# --- mismatched metadata is flagged --------------------------------------------------


def test_trigger_rule_on_an_llm_agent_warns(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        agent = parse_agent_file(
            write_agent(
                tmp_path,
                "a",
                'provider: anthropic\nmodel: claude-opus-5\n'
                'triggerRule: \'text.contains("A")\'\npriority: 50\n',
            )
        )

    assert agent.trigger_rule is None  # never consulted for an llm agent
    assert "only apply to 'script' agents" in caplog.text
    assert "triggerRule" in caplog.text


def test_mcp_on_a_script_agent_warns(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        parse_agent_file(
            write_agent(
                tmp_path,
                "a",
                "type: script\nscript: handler.py\nmcp:\n  - name: x\n    command: uvx\n",
                SIMPLE,
            )
        )

    assert "only apply to 'llm' agents" in caplog.text
    assert "mcp" in caplog.text


# --- loading the entry point ---------------------------------------------------------


def load(tmp_path, script: str, name: str = "a"):
    return load_entry_point(
        parse_agent_file(
            write_agent(tmp_path, name, "type: script\nscript: handler.py\n", script)
        )
    )


def test_entry_point_is_returned(tmp_path):
    assert callable(load(tmp_path, SIMPLE))


def test_missing_run_function_is_reported(tmp_path):
    with pytest.raises(ScriptLoadError, match="no `run\\(\\)` function"):
        load(tmp_path, "def other(): pass\n")


def test_run_that_is_not_callable_is_reported(tmp_path):
    with pytest.raises(ScriptLoadError, match="not callable"):
        load(tmp_path, "run = 42\n")


def test_import_time_error_is_reported_at_load(tmp_path):
    with pytest.raises(ScriptLoadError, match="failed to import"):
        load(tmp_path, "raise ValueError('boom at import')\n")


def test_syntax_error_is_reported_at_load(tmp_path):
    with pytest.raises(ScriptLoadError, match="failed to import"):
        load(tmp_path, "def run(:\n")


def test_two_agents_may_ship_the_same_filename(tmp_path):
    """Module names are namespaced, so handler.py in two agents must not collide."""
    first = load(tmp_path, "def run(m):\n    return 'first'\n", name="one")
    second = load(tmp_path, "def run(m):\n    return 'second'\n", name="two")
    assert first({}) == "first"
    assert second({}) == "second"


# --- executing run() -----------------------------------------------------------------


async def execute(tmp_path, script: str, payload: dict | None = None, **overrides):
    extra = "".join(f"{key}: {value}\n" for key, value in overrides.items())
    agent = parse_agent_file(
        write_agent(tmp_path, "a", f"type: script\nscript: handler.py\n{extra}", script)
    )
    runner = ScriptRunner(agent, load_entry_point(agent))
    return await runner.run(payload or {"text": "hi"})


async def test_sync_run(tmp_path):
    result = await execute(tmp_path, "def run(message):\n    return 'sync ok'\n")
    assert result.output == "sync ok"
    assert result.succeeded


async def test_async_run(tmp_path):
    result = await execute(tmp_path, "async def run(message):\n    return 'async ok'\n")
    assert result.output == "async ok"
    assert result.succeeded


async def test_payload_reaches_the_script(tmp_path):
    result = await execute(
        tmp_path,
        "def run(message):\n    return message['text'] + '|' + str(message['channel'])\n",
        payload={"text": "hello", "channel": "C1"},
    )
    assert result.output == "hello|C1"


async def test_dict_return_is_serialised(tmp_path):
    result = await execute(tmp_path, "def run(message):\n    return {'a': 1}\n")
    assert '"a": 1' in result.output


async def test_none_return_is_empty(tmp_path):
    result = await execute(tmp_path, "def run(message):\n    return None\n")
    assert result.output == ""
    assert result.succeeded


async def test_non_string_return_is_stringified(tmp_path):
    result = await execute(tmp_path, "def run(message):\n    return 42\n")
    assert result.output == "42"


async def test_exception_becomes_an_error_not_a_raise(tmp_path):
    result = await execute(tmp_path, "def run(message):\n    raise ValueError('nope')\n")
    assert result.succeeded is False
    assert "ValueError: nope" in (result.error or "")
    assert result.output == ""


async def test_timeout_is_reported(tmp_path):
    """The sleep is kept short on purpose.

    A timed-out sync `run()` is abandoned, not killed, so its thread keeps running and
    the interpreter joins it at exit — a 30s sleep here would add 30s to the suite.
    """
    result = await execute(
        tmp_path,
        "import time\ndef run(message):\n    time.sleep(3)\n",
        timeout=1,
    )
    assert result.succeeded is False
    assert "timed out after 1s" in (result.error or "")


async def test_timed_out_async_run_is_actually_cancelled(tmp_path):
    """An async `run()` can be cancelled properly, unlike a blocking sync one."""
    result = await execute(
        tmp_path,
        "import asyncio\nasync def run(message):\n    await asyncio.sleep(30)\n",
        timeout=1,
    )
    assert "timed out after 1s" in (result.error or "")


async def test_long_output_is_truncated(tmp_path):
    result = await execute(tmp_path, "def run(message):\n    return 'x' * 50000\n")
    assert "truncated" in result.output
    assert len(result.output) < 30_000


async def test_result_carries_agent_name_and_priority(tmp_path):
    result = await execute(tmp_path, SIMPLE, priority=250)
    assert result.agent == "a"
    assert result.priority == 250
