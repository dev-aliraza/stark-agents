"""Getting images to a model without caring which model it is.

Three problems, and none of them needs provider-specific code:

**Can this model see at all?** `litellm.supports_vision` answers from LiteLLM's capability
map, so a text-only model is detected before it is ever sent a picture rather than after it
returns an error.

**How does an image reach it?** As an ordinary user message using OpenAI's `image_url`
blocks, which LiteLLM translates per provider. Not inside the tool result: Anthropic permits
images there, OpenAI requires `role: "tool"` content to be a string, and building on that
difference would mean owning a branch forever. LiteLLM merges the tool message and the
following user message into one turn for Anthropic — `[tool_result, text, image]`, exactly
the shape that API wants — and hands Gemini `[function_response]` then `[text, inline_data]`.

**What stops it becoming ruinous?** Every tool result is re-sent on every later turn, so ten
screenshots in a task is not ten images, it is fifty-five. `prune_images` keeps the last few
and leaves a text stub where the others were.

One thing `supports_vision` cannot tell you, and it matters: it reports whether a model
accepts images, not whether it is any good at saying *where* something is in one. Reading an
image is a commodity capability; coordinate grounding is not, and it degrades quietly on
smaller models rather than failing. That is why the browser toolset keeps refs as the normal
way to act, and treats coordinates as the fallback for pages that have no DOM worth reading.
"""

from __future__ import annotations

from typing import Any

from .logger import get_logger
from .types import ToolImage

logger = get_logger("vision")

# How many images stay in the conversation. Two covers "before and after this click" — the
# comparison a model actually makes — without carrying a whole session's screenshots.
DEFAULT_IMAGES_KEPT = 2

_PRUNED_NOTE = "[screenshot from an earlier step, no longer shown]"


def model_can_see(provider: str, model: str) -> bool:
    """Whether this model accepts images.

    Unknown models answer False. That is the safer direction: a model wrongly believed to be
    text-only loses a tool it might have used, while one wrongly believed to see gets an API
    error mid-task, which is much harder to read back to a cause.
    """
    from .llm.client import qualified_model

    name = qualified_model(provider, model)
    try:
        import litellm

        return bool(litellm.supports_vision(model=name))
    except Exception as exc:  # pragma: no cover - capability map lookup is best-effort
        logger.debug("Could not determine whether '%s' supports vision: %s", name, exc)
        return False


def image_message(images: list[ToolImage]) -> dict[str, Any] | None:
    """One user message carrying every image a turn produced, or None if there were none."""
    if not images:
        return None

    content: list[dict[str, Any]] = []
    for image in images:
        if image.label:
            content.append({"type": "text", "text": image.label})
        content.append(image.as_content_block())
    return {"role": "user", "content": content}


def prune_images(messages: list[dict[str, Any]], keep: int = DEFAULT_IMAGES_KEPT) -> int:
    """Replace all but the last `keep` images with a text stub. Returns how many went.

    Mutates the list in place, because it is the live conversation. Order is preserved and
    only image blocks are touched, so a message that carried both a label and a picture keeps
    its label — the model can still see that a screenshot happened there, and when.

    This is pure message-list surgery above LiteLLM, which is what makes it work the same on
    every provider.
    """
    positions = [
        (index, block_index)
        for index, message in enumerate(messages)
        if isinstance(message.get("content"), list)
        for block_index, block in enumerate(message["content"])
        if isinstance(block, dict) and block.get("type") == "image_url"
    ]

    stale = positions[:-keep] if keep > 0 else positions
    for index, block_index in stale:
        messages[index]["content"][block_index] = {"type": "text", "text": _PRUNED_NOTE}
    return len(stale)
