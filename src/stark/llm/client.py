from __future__ import annotations

from typing import Any, Awaitable, Callable, Sequence

import litellm

from ..logger import get_logger
from ..types import Completion, ToolCall

logger = get_logger("llm")

# Providers vary in which knobs they accept (reasoning_effort, parallel_tool_calls,
# ...). Dropping unsupported params keeps one call path working across all of them
# instead of special-casing each provider.
litellm.drop_params = True
litellm.suppress_debug_info = True

TextCallback = Callable[[str], Awaitable[None]]


def qualified_model(provider: str, model: str) -> str:
    """Return the LiteLLM model string, e.g. ("anthropic", "claude-opus-5")."""
    if not provider or "/" in model:
        return model
    return f"{provider}/{model}"


def _cost(response: Any) -> float:
    try:
        return float(litellm.completion_cost(completion_response=response) or 0.0)
    except Exception:  # pragma: no cover - pricing data is best-effort
        return 0.0


class LLMClient:
    """A thin, provider-agnostic wrapper over `litellm.acompletion`."""

    @staticmethod
    def _build_kwargs(
        *,
        provider: str,
        model: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None,
        effort: str | None,
        max_output_tokens: int | None,
        base_url: str,
        api_key: str,
        parallel_tool_calls: bool,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": qualified_model(provider, model),
            "messages": list(messages),
        }
        if tools:
            kwargs["tools"] = list(tools)
            kwargs["tool_choice"] = "auto"
            if parallel_tool_calls:
                kwargs["parallel_tool_calls"] = True
        if effort and effort.lower() != "none":
            kwargs["reasoning_effort"] = effort.lower()
        if max_output_tokens:
            kwargs["max_tokens"] = max_output_tokens
        if base_url:
            kwargs["api_base"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        return kwargs

    @classmethod
    async def complete(
        cls,
        *,
        provider: str,
        model: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        effort: str | None = None,
        max_output_tokens: int | None = None,
        base_url: str = "",
        api_key: str = "",
        parallel_tool_calls: bool = True,
        on_text: TextCallback | None = None,
    ) -> Completion:
        """Run one model turn.

        When `on_text` is supplied the request is streamed and each text delta is
        handed to the callback as it arrives; the return value is identical either
        way, so callers only choose whether they want incremental output.
        """
        kwargs = cls._build_kwargs(
            provider=provider,
            model=model,
            messages=messages,
            tools=tools,
            effort=effort,
            max_output_tokens=max_output_tokens,
            base_url=base_url,
            api_key=api_key,
            parallel_tool_calls=parallel_tool_calls,
        )

        if on_text is None:
            response = await litellm.acompletion(**kwargs)
            return cls._parse(response)

        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}
        stream = await litellm.acompletion(**kwargs)
        return await cls._consume_stream(stream, kwargs["messages"], on_text)

    @staticmethod
    def _parse(response: Any) -> Completion:
        completion = Completion(cost=_cost(response))

        choices = getattr(response, "choices", None) or []
        if not choices:
            return completion

        choice = choices[0]
        completion.finish_reason = getattr(choice, "finish_reason", None)
        message = getattr(choice, "message", None)
        if message is None:
            return completion

        content = getattr(message, "content", None)
        if content:
            completion.content = content

        for raw in getattr(message, "tool_calls", None) or []:
            function = getattr(raw, "function", None)
            completion.tool_calls.append(
                ToolCall(
                    id=getattr(raw, "id", "") or "",
                    name=getattr(function, "name", "") or "",
                    arguments=getattr(function, "arguments", "") or "",
                )
            )

        return completion

    @staticmethod
    async def _consume_stream(
        stream: Any,
        messages: list[dict[str, Any]],
        on_text: TextCallback,
    ) -> Completion:
        completion = Completion()
        # Tool calls arrive in fragments keyed by index: the id and name land on the
        # first fragment, the arguments accumulate across the rest.
        pending: dict[int, ToolCall] = {}
        chunks: list[Any] = []

        async for chunk in stream:
            chunks.append(chunk)
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue

            choice = choices[0]
            if getattr(choice, "finish_reason", None):
                completion.finish_reason = choice.finish_reason

            delta = getattr(choice, "delta", None)
            if delta is None:
                continue

            text = getattr(delta, "content", None)
            if text:
                completion.content += text
                await on_text(text)

            for fragment in getattr(delta, "tool_calls", None) or []:
                index = getattr(fragment, "index", 0) or 0
                function = getattr(fragment, "function", None)
                call = pending.get(index)
                if call is None:
                    call = ToolCall(id="", name="")
                    pending[index] = call
                if getattr(fragment, "id", None):
                    call.id = fragment.id
                if function is not None:
                    if getattr(function, "name", None):
                        call.name = function.name
                    if getattr(function, "arguments", None):
                        call.arguments += function.arguments

        completion.tool_calls = [pending[index] for index in sorted(pending)]

        try:
            rebuilt = litellm.stream_chunk_builder(chunks, messages=messages)
            completion.cost = _cost(rebuilt)
        except Exception:  # pragma: no cover - usage data is best-effort
            completion.cost = 0.0

        return completion
