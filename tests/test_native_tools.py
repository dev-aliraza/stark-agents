"""The `tools:` block: declaring native capabilities per agent, and for the orchestrator.

Native tools are Stark's own, running in-process. `mcp:` remains for third-party servers.
`file` is the one handed to everybody without being asked for, because it is confined to a
single directory.
"""

from __future__ import annotations

import pytest

from stark.config import Config, ConfigError, OrchestratorConfig
from stark.orchestration import Orchestrator, Registry, ToolBox, build_toolsets
from stark.parsers import discover_agents, parse_agent_file
from stark.tools import ALWAYS_ON, CATALOG, TOOL_NAMES, ToolFilter, known_settings, spec_for
from stark.types import ModelConfig, ToolConfig, with_always_on

LLM_AGENT = """\
---
name: {name}
description: An agent.
provider: anthropic
model: claude-opus-5
{extra}---

Body.
"""


def write_agent(root, name: str = "one", extra: str = ""):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "AGENT.md").write_text(
        LLM_AGENT.format(name=name, extra=extra), encoding="utf-8"
    )
    return directory / "AGENT.md"


def tools_of(root, name: str = "one") -> dict[str, ToolConfig]:
    agents = {agent.name: agent for agent in discover_agents(root)}
    return {tool.name: tool for tool in agents[name].tools}


# --- the catalog ------------------------------------------------------------------------


def test_the_catalog_covers_the_documented_tools():
    assert set(TOOL_NAMES) == {"file", "shell", "websearch"}


def test_file_is_the_only_always_on_tool():
    """It is confined to one directory; a shell and a browser are not."""
    assert ALWAYS_ON == ("file",)


def test_every_spec_can_be_constructed_with_a_root_and_settings(tmp_path):
    """Every native toolset takes `(root, settings)`.

    This is the contract `build_toolsets` calls, and a mismatch there is invisible: the
    failure is logged and the toolset silently missing. Catch it here instead.
    """
    for name, spec in CATALOG.items():
        if spec.extras:
            pytest.importorskip("httpx", reason=f"the {name} tool needs an extra")
        instance = spec.load()(tmp_path, {})

        assert instance.schemas(), f"{name} offers no tools"
        assert instance.owns(instance.schemas()[0]["function"]["name"]) is True
        assert instance.owns("definitely_not_mine") is False


async def test_every_spec_closes_cleanly_unused(tmp_path):
    for name, spec in CATALOG.items():
        if spec.extras:
            pytest.importorskip("httpx", reason=f"the {name} tool needs an extra")
        await spec.load()(tmp_path, {}).aclose()


def test_tool_names_do_not_collide_across_toolsets(tmp_path):
    """Two toolsets claiming one name would make routing ambiguous."""
    seen: dict[str, str] = {}
    for name, spec in CATALOG.items():
        if spec.extras:
            pytest.importorskip("httpx", reason=f"the {name} tool needs an extra")
        for schema in spec.load()(tmp_path, {}).schemas():
            tool = schema["function"]["name"]
            assert tool not in seen, f"{name} and {seen.get(tool)} both offer {tool}"
            seen[tool] = name


def test_an_unknown_tool_has_no_spec():
    assert spec_for("desktop") is None
    assert known_settings("desktop") == ()


def test_a_missing_dependency_names_the_extra_to_install():
    assert CATALOG["websearch"].extras == ("websearch",)


# --- parsing the block ------------------------------------------------------------------


def test_no_tools_block_still_yields_file(tmp_path):
    write_agent(tmp_path)
    agent = discover_agents(tmp_path)[0]

    assert agent.tools == []
    assert [tool.name for tool in agent.enabled_tools] == ["file"]


def test_a_list_of_names_is_accepted(tmp_path):
    write_agent(tmp_path, extra="tools: [shell, websearch]\n")
    declared = tools_of(tmp_path)

    assert set(declared) == {"shell", "websearch"}
    assert declared["shell"].settings == {}


def test_a_mapping_with_no_settings_is_accepted(tmp_path):
    """`shell:` with nothing after it parses to None, which must mean defaults."""
    write_agent(tmp_path, extra="tools:\n  shell:\n  websearch: {}\n")
    declared = tools_of(tmp_path)

    assert set(declared) == {"shell", "websearch"}
    assert declared["shell"].settings == {}


def test_settings_are_parsed(tmp_path):
    write_agent(
        tmp_path,
        extra="tools:\n  shell:\n    allow: [git, ls]\n    timeout: 30\n",
    )
    shell = tools_of(tmp_path)["shell"]

    assert shell.settings == {"allow": ["git", "ls"], "timeout": 30}


def test_include_and_exclude_are_parsed(tmp_path):
    write_agent(
        tmp_path,
        extra="tools:\n  websearch:\n    exclude: [websearch_open]\n",
    )
    websearch = tools_of(tmp_path)["websearch"]

    assert websearch.exclude == ["websearch_open"]
    assert websearch.include == []


def test_a_single_string_is_accepted_for_exclude(tmp_path):
    write_agent(tmp_path, extra="tools:\n  websearch:\n    exclude: websearch_open\n")
    assert tools_of(tmp_path)["websearch"].exclude == ["websearch_open"]


def test_enable_false_removes_a_tool(tmp_path):
    write_agent(tmp_path, extra="tools:\n  shell:\n    enable: false\n    allow: [git]\n")
    agent = discover_agents(tmp_path)[0]

    # Still declared, so the config is preserved and merely parked.
    assert agent.tools[0].enable is False
    assert [tool.name for tool in agent.enabled_tools] == ["file"]


def test_file_can_be_switched_off(tmp_path):
    """The only way to make an agent that cannot touch files at all."""
    write_agent(tmp_path, extra="tools:\n  file:\n    enable: false\n")
    assert discover_agents(tmp_path)[0].enabled_tools == []


def test_file_can_be_narrowed_to_read_only(tmp_path):
    write_agent(
        tmp_path,
        extra="tools:\n  file:\n    exclude: [file_write, file_delete, file_run]\n",
    )
    agent = discover_agents(tmp_path)[0]
    declared = {tool.name: tool for tool in agent.enabled_tools}

    assert declared["file"].exclude == ["file_write", "file_delete", "file_run"]


def test_an_unknown_tool_is_warned_about_and_dropped(tmp_path, caplog):
    write_agent(tmp_path, extra="tools:\n  desktop:\n    clicks: yes\n")

    with caplog.at_level("WARNING"):
        agent = discover_agents(tmp_path)[0]

    assert agent.tools == []
    assert "unknown tool 'desktop'" in caplog.text
    # The agent still loads: one bad key costs you that key, not the agent.
    assert agent.name == "one"


def test_an_unknown_setting_is_warned_about_and_ignored(tmp_path, caplog):
    write_agent(tmp_path, extra="tools:\n  shell:\n    allowlist: [git]\n")

    with caplog.at_level("WARNING"):
        shell = tools_of(tmp_path)["shell"]

    assert "no setting(s) allowlist" in caplog.text
    assert shell.settings == {}


def test_the_warning_lists_the_valid_settings(tmp_path, caplog):
    write_agent(tmp_path, extra="tools:\n  shell:\n    nope: 1\n")

    with caplog.at_level("WARNING"):
        tools_of(tmp_path)

    for setting in ("allow", "cwd", "timeout"):
        assert setting in caplog.text


def test_a_wrong_shape_is_warned_about(tmp_path, caplog):
    write_agent(tmp_path, extra="tools: 42\n")

    with caplog.at_level("WARNING"):
        agent = discover_agents(tmp_path)[0]

    assert agent.tools == []
    assert "must be a list of names or a mapping" in caplog.text


def test_settings_expand_environment_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_PATH", "/tmp/somewhere")
    write_agent(tmp_path, extra="tools:\n  shell:\n    cwd: ${REPO_PATH:-.}\n")

    assert tools_of(tmp_path)["shell"].settings["cwd"] == "/tmp/somewhere"


def test_tools_is_ignored_on_a_script_agent(tmp_path, caplog):
    """A script agent has no toolbox at all, so `tools:` there does nothing."""
    directory = tmp_path / "scripted"
    directory.mkdir()
    (directory / "handler.py").write_text("def run(m):\n    return 'x'\n", encoding="utf-8")
    (directory / "AGENT.md").write_text(
        "---\nname: scripted\ndescription: d\ntype: script\nscript: handler.py\n"
        "tools: [shell]\n---\n\nBody.\n",
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        agent = discover_agents(tmp_path)[0]

    assert agent.tools == []
    assert "'tools'" in caplog.text


# --- with_always_on ---------------------------------------------------------------------


def test_always_on_is_added_when_undeclared():
    assert [tool.name for tool in with_always_on([])] == ["file"]


def test_a_declared_entry_wins_over_the_default():
    declared = ToolConfig(name="file", exclude=["file_delete"])
    result = with_always_on([declared])

    assert result == [declared]


def test_declaration_order_is_preserved():
    tools = [ToolConfig(name="shell"), ToolConfig(name="browser")]
    assert [tool.name for tool in with_always_on(tools)] == ["shell", "browser", "file"]


# --- the filter --------------------------------------------------------------------------


def test_a_filter_with_nothing_set_allows_everything():
    assert ToolFilter().allows("anything") is True


def test_exclude_removes_a_name():
    assert ToolFilter(exclude=["b"]).allows("b") is False
    assert ToolFilter(exclude=["b"]).allows("a") is True


def test_include_is_an_allowlist():
    filter = ToolFilter(include=["a"])
    assert filter.allows("a") is True
    assert filter.allows("b") is False


def test_exclude_beats_include():
    assert ToolFilter(include=["a"], exclude=["a"]).allows("a") is False


# --- composition in the toolbox ----------------------------------------------------------


async def test_a_toolbox_merges_its_toolsets(tmp_path):
    box = ToolBox(build_toolsets(with_always_on([ToolConfig(name="shell")]), tmp_path, "test"))
    names = {schema["function"]["name"] for schema in box.schemas()}

    assert {"file_read", "shell_run"} <= names


async def test_excluded_tools_are_neither_offered_nor_callable(tmp_path):
    """A model that guessed the name must not reach past the filter."""
    box = ToolBox(
        build_toolsets(
            [ToolConfig(name="file", exclude=["file_delete"])], tmp_path, "test"
        )
    )

    assert box.offers("file_delete") is False
    assert "unknown tool" in await box.call("file_delete", {"path": "x"})
    # And the rest still works.
    assert box.offers("file_list") is True
    (tmp_path / "notes.md").write_text("hi", encoding="utf-8")
    assert "notes.md" in await box.call("file_list", {})


async def test_an_unbuildable_toolset_is_logged_and_skipped(tmp_path, caplog):
    """One unavailable capability should not stop the agent loading."""
    from stark.tools.catalog import ToolSpec

    broken = ToolSpec(name="broken", module="stark.tools.nonexistent", factory="Nope")
    with caplog.at_level("ERROR"):
        built = build_toolsets([ToolConfig(name="broken")], tmp_path, "test")

    # spec_for('broken') is None, so nothing is built and nothing raises.
    assert built == []
    assert broken.name == "broken"


async def test_toolsets_are_per_agent_not_shared(tmp_path):
    """Two agents with the same tool must not share its state or its config."""
    strict = build_toolsets(
        [ToolConfig(name="shell", settings={"allow": ["git"]})], tmp_path, "a"
    )
    open_ended = build_toolsets([ToolConfig(name="shell")], tmp_path, "b")

    assert strict[0][0] is not open_ended[0][0]
    assert strict[0][0].allow == ("git",)
    assert open_ended[0][0].allow == ()


async def test_a_registry_agent_gets_its_declared_tools(tmp_path):
    write_agent(tmp_path, "worker", extra="tools:\n  shell:\n    allow: [ls]\n")

    registry = await Registry.create(tmp_path)
    try:
        names = {
            schema["function"]["name"]
            for schema in registry.toolbox_for(registry.llm_agents[0]).schemas()
        }
        assert {"shell_run", "file_read"} <= names
    finally:
        await registry.aclose()


# --- the orchestrator's own tools ---------------------------------------------------------


def test_the_orchestrator_config_defaults_to_nothing_declared():
    settings = OrchestratorConfig()
    assert settings.tools == {}
    assert settings.root == ""


def test_the_orchestrator_config_is_reached_through_run_config():
    config = Config.coerce({"orchestrator": {"tools": {"shell": {"allow": ["git"]}}}})
    assert config.orchestrator.tools == {"shell": {"allow": ["git"]}}


def test_an_unknown_orchestrator_key_is_rejected():
    """Unlike AGENT.md, `config` is strict: a silently ignored typo is a setting that never applies."""
    with pytest.raises(ConfigError, match="unknown config.orchestrator key"):
        Config.coerce({"orchestrator": {"tool": {}}})


def test_a_wrong_type_for_orchestrator_tools_is_rejected():
    with pytest.raises(ConfigError, match="config.orchestrator.tools must be"):
        Config.coerce({"orchestrator": {"tools": "shell"}})


def test_a_wrong_type_for_the_root_is_rejected():
    with pytest.raises(ConfigError, match="config.orchestrator.root must be"):
        Config.coerce({"orchestrator": {"root": 42}})


def test_the_orchestrator_tool_list_includes_file_by_default():
    from stark.runtime import orchestrator_tools

    names = [tool.name for tool in orchestrator_tools(OrchestratorConfig())]
    assert names == ["file"]


def test_the_orchestrator_can_declare_more():
    from stark.runtime import orchestrator_tools

    settings = OrchestratorConfig(tools={"shell": {"allow": ["git"]}})
    names = {tool.name for tool in orchestrator_tools(settings)}
    assert names == {"file", "shell"}


def test_the_orchestrator_can_have_file_switched_off():
    from stark.runtime import orchestrator_tools

    settings = OrchestratorConfig(tools={"file": {"enable": False}})
    assert orchestrator_tools(settings) == []


async def test_the_orchestrator_advertises_its_own_tools(tmp_path):
    write_agent(tmp_path, "worker")

    registry = await Registry.create(tmp_path)
    try:
        box = ToolBox(build_toolsets(with_always_on([]), tmp_path, "Orchestrator"))
        orchestrator = Orchestrator(registry, "Be helpful.", ModelConfig(), box)
        names = {
            schema["function"]["name"]
            for schema in registry.delegation_tools() + orchestrator._own_tools()
        }

        assert "agent__worker" in names
        assert "file_read" in names
    finally:
        await registry.aclose()


async def test_a_delegation_only_orchestrator_advertises_nothing_extra(tmp_path):
    write_agent(tmp_path, "worker")

    registry = await Registry.create(tmp_path)
    try:
        orchestrator = Orchestrator(registry, "Be helpful.", ModelConfig())
        assert orchestrator._own_tools() == []
        assert "Your own tools" not in orchestrator.system_prompt()
    finally:
        await registry.aclose()


async def test_the_prompt_gains_a_section_when_it_has_tools(tmp_path):
    write_agent(tmp_path, "worker")

    registry = await Registry.create(tmp_path)
    try:
        box = ToolBox(build_toolsets(with_always_on([]), tmp_path, "Orchestrator"))
        prompt = Orchestrator(registry, "Be helpful.", ModelConfig(), box).system_prompt()

        assert "## Your own tools" in prompt
        # The context cost is the reason to prefer an agent, so it has to be stated.
        assert "sent again on every later turn" in prompt
        # And it still must not narrate which route it took.
        assert "Say nothing about which of the two you used" in prompt
    finally:
        await registry.aclose()
