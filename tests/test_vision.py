"""Images on their way to a model, and the claim that it works on any of them.

The load-bearing assertion in this file is `test_the_image_shape_survives_translation_to_*`.
Everything else here is plumbing; those two are the reason the feature can be called
model-agnostic at all. If a LiteLLM upgrade changes how it translates an image block, the
browser toolset silently stops working on that provider — so the translation is pinned here
rather than trusted.
"""

from __future__ import annotations

import pytest

from stark.types import ToolImage, ToolResult
from stark.vision import (
    DEFAULT_IMAGES_KEPT,
    image_message,
    model_can_see,
    prune_images,
)

PIXEL = "iVBORw0KGgoAAAANSUhEUg=="


def shot(label: str = "") -> ToolImage:
    return ToolImage(data=PIXEL, label=label)


def conversation(images: int) -> list[dict]:
    """A conversation with `images` screenshots in it, shaped like a real agent loop."""
    messages: list[dict] = [{"role": "user", "content": "go"}]
    for index in range(images):
        messages.append({"role": "assistant", "content": f"looking, step {index}"})
        messages.append({"role": "tool", "tool_call_id": f"t{index}", "content": "{}"})
        messages.append(image_message([shot(f"Screenshot {index}:")]))
    return messages


# --- can this model see? ------------------------------------------------------------------


def test_a_vision_model_is_recognised():
    assert model_can_see("anthropic", "claude-opus-5") is True


def test_a_text_only_model_is_recognised():
    assert model_can_see("deepseek", "deepseek-chat") is False


def test_an_unknown_model_is_assumed_blind():
    """The safe direction: losing a tool beats an API error nobody can trace."""
    assert model_can_see("anthropic", "some-model-that-does-not-exist") is False


# --- the message an image travels in ------------------------------------------------------


def test_no_images_produces_no_message():
    """A turn that took no screenshot must not append an empty user message."""
    assert image_message([]) is None


def test_an_image_becomes_an_openai_style_block():
    message = image_message([shot()])

    assert message["role"] == "user"
    block = message["content"][0]
    assert block["type"] == "image_url"
    assert block["image_url"]["url"] == f"data:image/png;base64,{PIXEL}"


def test_a_label_precedes_its_image():
    """Two screenshots in one turn are indistinguishable without them."""
    content = image_message([shot("Screenshot of tab 42:")])["content"]

    assert content[0] == {"type": "text", "text": "Screenshot of tab 42:"}
    assert content[1]["type"] == "image_url"


def test_several_images_share_one_message():
    content = image_message([shot("first:"), shot("second:")])["content"]
    assert [block["type"] for block in content] == ["text", "image_url", "text", "image_url"]


# --- the provider-agnostic claim ----------------------------------------------------------


def test_the_image_shape_survives_translation_to_anthropic():
    """LiteLLM must merge the tool result and the image into one turn.

    Anthropic wants `[tool_result, text, image]` in a single user turn. If LiteLLM ever
    stopped merging, this would become two consecutive user messages — which is the failure
    mode that decided the whole design, so it is pinned.
    """
    litellm = pytest.importorskip("litellm")

    translated = litellm.AnthropicConfig().transform_request(
        model="claude-opus-5",
        messages=[
            {"role": "user", "content": "look at it"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "browser_screenshot", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "{}"},
            image_message([shot("Screenshot of tab 42:")]),
        ],
        optional_params={},
        litellm_params={},
        headers={},
    )

    blocks = [block.get("type") for block in translated["messages"][-1]["content"]]
    assert blocks == ["tool_result", "text", "image"]


def test_the_image_shape_survives_translation_to_gemini():
    """A different provider, a different native format, the same input from us."""
    pytest.importorskip("litellm")
    from litellm.llms.vertex_ai.gemini.transformation import (
        _gemini_convert_messages_with_history,
    )

    turns = _gemini_convert_messages_with_history(
        messages=[{"role": "user", "content": "look"}, image_message([shot("Shot:")])]
    )

    # Gemini folds consecutive user turns together, so the label and the picture land at the
    # end of one turn rather than in a turn of their own. What matters is that the data URL
    # became native `inline_data` instead of being passed through as text.
    parts = [key for part in turns[-1]["parts"] for key in part]
    assert parts[-2:] == ["text", "inline_data"]


# --- keeping the conversation affordable ---------------------------------------------------


def test_recent_images_are_kept():
    messages = conversation(4)
    prune_images(messages, keep=2)

    surviving = [
        block
        for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "image_url"
    ]
    assert len(surviving) == 2


def test_older_images_become_a_stub_rather_than_vanishing():
    """The model should still see that a screenshot happened there, and when."""
    messages = conversation(3)
    prune_images(messages, keep=1)

    first = messages[3]["content"]
    assert first[0] == {"type": "text", "text": "Screenshot 0:"}  # the label stays
    assert first[1]["type"] == "text" and "earlier step" in first[1]["text"]


def test_pruning_reports_what_it_dropped():
    assert prune_images(conversation(5), keep=2) == 3


def test_pruning_is_idempotent():
    """It runs after every turn, so a second pass must not eat the survivors."""
    messages = conversation(4)
    prune_images(messages, keep=2)
    assert prune_images(messages, keep=2) == 0


def test_a_short_conversation_is_left_alone():
    assert prune_images(conversation(1), keep=DEFAULT_IMAGES_KEPT) == 0


def test_text_only_conversations_are_untouched():
    messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    before = [dict(message) for message in messages]

    assert prune_images(messages) == 0
    assert messages == before


# --- what a toolset returns ----------------------------------------------------------------


def test_a_tool_result_carries_text_and_images():
    result = ToolResult(text="{}", images=[shot()])
    assert result.text == "{}" and len(result.images) == 1


def test_a_tool_result_defaults_to_no_images():
    assert ToolResult(text="fine").images == []


# --- the seam in the agent loop -------------------------------------------------------------


class SeeingToolset:
    """A toolset with one text tool and one that also returns a picture."""

    def schemas(self):
        return [
            {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}
            for name in ("look", "read")
        ]

    def owns(self, tool_name):
        return tool_name in {"look", "read"}

    def needs_vision(self, tool_name):
        return tool_name == "look"

    async def call(self, tool_name, arguments):
        if tool_name == "look":
            return ToolResult(text='{"width": 1400}', images=[shot("Screenshot:")])
        return "just text"

    async def aclose(self):
        return None


def toolbox(vision: bool):
    from stark.orchestration import ToolBox
    from stark.tools import ToolFilter

    return ToolBox([(SeeingToolset(), ToolFilter())], None, vision=vision)


def test_a_seeing_model_is_offered_the_image_tool():
    assert {s["function"]["name"] for s in toolbox(vision=True).schemas()} == {"look", "read"}


def test_a_blind_model_is_not_offered_it():
    """Offering a tool whose result it cannot perceive would waste a turn discovering that."""
    assert {s["function"]["name"] for s in toolbox(vision=False).schemas()} == {"read"}


def test_a_withheld_tool_cannot_be_reached_by_guessing_its_name():
    async def check():
        return await toolbox(vision=False).call("look", {})

    import asyncio

    assert "unknown tool" in asyncio.run(check())


def test_the_withholding_is_logged_with_the_fix(caplog):
    with caplog.at_level("WARNING"):
        toolbox(vision=False)
    assert "cannot accept images" in caplog.text and "look" in caplog.text


def test_toolsets_without_the_hook_are_unaffected():
    """Only `browser` implements `needs_vision`; everything else must be untouched by it."""
    from stark.orchestration import ToolBox
    from stark.tools import ToolFilter
    from stark.tools.file import FileTools

    blind = ToolBox([(FileTools(".", {}), ToolFilter())], None, vision=False)
    assert {s["function"]["name"] for s in blind.schemas()} >= {"file_read", "file_write"}


async def test_the_agent_loop_attaches_images_after_the_tool_result():
    """The ordering that makes LiteLLM merge them into one Anthropic turn."""
    from stark.orchestration.agent_runner import AgentRunner
    from stark.types import AgentConfig, ToolCall

    class Sink:
        async def event(self, *args, **kwargs):
            return None

    runner = AgentRunner(AgentConfig(name="a", description="d", instructions="", path="."), toolbox(vision=True))
    messages = await runner._run_tools(
        [ToolCall(id="c1", name="look", arguments="{}")], Sink(), "key"
    )

    assert [message["role"] for message in messages] == ["tool", "user"]
    assert messages[0]["content"] == '{"width": 1400}'
    assert messages[1]["content"][-1]["type"] == "image_url"


async def test_a_text_only_turn_appends_no_user_message():
    from stark.orchestration.agent_runner import AgentRunner
    from stark.types import AgentConfig, ToolCall

    class Sink:
        async def event(self, *args, **kwargs):
            return None

    runner = AgentRunner(AgentConfig(name="a", description="d", instructions="", path="."), toolbox(vision=True))
    messages = await runner._run_tools(
        [ToolCall(id="c1", name="read", arguments="{}")], Sink(), "key"
    )

    assert [message["role"] for message in messages] == ["tool"]


# --- the system prompt must describe the tools that exist ----------------------------------


def runner_with(tool_names):
    """An AgentRunner whose toolbox offers exactly these tools."""
    from stark.orchestration import ToolBox
    from stark.orchestration.agent_runner import AgentRunner
    from stark.tools import ToolFilter
    from stark.types import AgentConfig

    class Named:
        def schemas(self):
            return [
                {"type": "function", "function": {"name": n, "description": "", "parameters": {}}}
                for n in tool_names
            ]

        def owns(self, name):
            return name in tool_names

        async def call(self, name, arguments):
            return ""

        async def aclose(self):
            return None

    config = AgentConfig(name="a", description="d", instructions="Do the thing.", path=".")
    return AgentRunner(config, ToolBox([(Named(), ToolFilter())], None))


def test_the_prompt_describes_the_file_tools_an_agent_has():
    prompt = runner_with(["file_list", "file_read", "file_write", "file_delete", "file_run"])._system_prompt()

    assert "## Your files" in prompt
    for tool in ("file_list", "file_read", "file_write", "file_delete", "file_run"):
        assert f"`{tool}`" in prompt


def test_an_agent_with_no_file_tools_is_told_nothing_about_them():
    """Naming tools it cannot call sends it reaching for them — the exact failure to avoid."""
    prompt = runner_with(["browser_screenshot", "browser_click_at"])._system_prompt()

    assert "## Your files" not in prompt
    assert "file_" not in prompt


def test_a_narrowed_file_toolset_is_described_narrowly():
    prompt = runner_with(["file_list", "file_read"])._system_prompt()

    assert "`file_list`" in prompt and "`file_read`" in prompt
    assert "file_write" not in prompt and "file_delete" not in prompt and "file_run" not in prompt
    # No warning about changing real files when it cannot change any.
    assert "Writing and deleting" not in prompt


def test_read_only_file_access_omits_the_script_advice():
    prompt = runner_with(["file_list", "file_read"])._system_prompt()
    assert "run one of your scripts" not in prompt


def test_the_reporting_section_is_always_present():
    """It is about the delegation contract, not about tools, so it never depends on them."""
    assert "## Reporting back" in runner_with([])._system_prompt()
