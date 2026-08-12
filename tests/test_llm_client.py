"""Tests for request building and stream accumulation in the model layer."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stark.llm import qualified_model
from stark.llm.client import LLMClient
from stark.types import Completion, ToolCall


def test_qualified_model_prefixes_the_provider():
    assert qualified_model("anthropic", "claude-opus-5") == "anthropic/claude-opus-5"


def test_qualified_model_leaves_prefixed_models_alone():
    assert qualified_model("anthropic", "openai/gpt-4o") == "openai/gpt-4o"
    assert qualified_model("", "claude-opus-5") == "claude-opus-5"


def test_build_kwargs_maps_effort_and_limits():
    kwargs = LLMClient._build_kwargs(
        provider="anthropic",
        model="claude-opus-5",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "t"}}],
        effort="high",
        max_output_tokens=2048,
        base_url="https://proxy/v1",
        api_key="key",
        parallel_tool_calls=True,
    )

    assert kwargs["model"] == "anthropic/claude-opus-5"
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["max_tokens"] == 2048
    assert kwargs["api_base"] == "https://proxy/v1"
    assert kwargs["api_key"] == "key"
    assert kwargs["tool_choice"] == "auto"
    assert kwargs["parallel_tool_calls"] is True


def test_build_kwargs_omits_effort_none_and_empty_tools():
    kwargs = LLMClient._build_kwargs(
        provider="anthropic",
        model="claude-opus-5",
        messages=[],
        tools=None,
        effort="none",
        max_output_tokens=None,
        base_url="",
        api_key="",
        parallel_tool_calls=True,
    )

    for absent in ("reasoning_effort", "max_tokens", "api_base", "api_key", "tools", "tool_choice"):
        assert absent not in kwargs


def test_as_message_with_content_only():
    assert Completion(content="hello").as_message() == {"role": "assistant", "content": "hello"}


def test_as_message_omits_empty_content_when_calling_tools():
    message = Completion(
        tool_calls=[ToolCall(id="c1", name="agent__x", arguments='{"task":"go"}')]
    ).as_message()

    assert "content" not in message
    assert message["tool_calls"][0]["function"] == {
        "name": "agent__x",
        "arguments": '{"task":"go"}',
    }


def test_parsed_arguments_tolerates_bad_json():
    assert ToolCall(id="c", name="n", arguments="not json").parsed_arguments() == {}
    assert ToolCall(id="c", name="n", arguments="").parsed_arguments() == {}
    assert ToolCall(id="c", name="n", arguments='{"a":1}').parsed_arguments() == {"a": 1}


def _chunk(*, text=None, tool_fragments=None, finish_reason=None):
    delta = SimpleNamespace(content=text, tool_calls=tool_fragments)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def _fragment(index, *, call_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


async def _stream(chunks):
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_stream_accumulates_text_and_fragmented_tool_calls(monkeypatch):
    # Model streams two parallel tool calls, with arguments split across chunks.
    chunks = [
        _chunk(text="thinking "),
        _chunk(tool_fragments=[_fragment(0, call_id="c0", name="agent__a", arguments='{"task"')]),
        _chunk(tool_fragments=[_fragment(1, call_id="c1", name="agent__b", arguments='{"task"')]),
        _chunk(tool_fragments=[_fragment(0, arguments=':"one"}')]),
        _chunk(tool_fragments=[_fragment(1, arguments=':"two"}')]),
        _chunk(text="out loud", finish_reason="tool_calls"),
    ]

    monkeypatch.setattr(
        "litellm.stream_chunk_builder", lambda chunks, messages=None: SimpleNamespace()
    )

    received: list[str] = []

    async def on_text(text: str) -> None:
        received.append(text)

    completion = await LLMClient._consume_stream(_stream(chunks), [], on_text)

    assert completion.content == "thinking out loud"
    assert received == ["thinking ", "out loud"]
    assert completion.finish_reason == "tool_calls"
    assert [(c.id, c.name, c.arguments) for c in completion.tool_calls] == [
        ("c0", "agent__a", '{"task":"one"}'),
        ("c1", "agent__b", '{"task":"two"}'),
    ]
