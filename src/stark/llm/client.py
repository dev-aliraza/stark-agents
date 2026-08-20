from __future__ import annotations

import asyncio
import random
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

# How many times to try again after the first attempt fails, and how long to wait between.
# Delays double — roughly 1, 2, 4, 8, 16 seconds — which is long enough for a rate limit to
# clear without stalling a run for minutes.
MAX_RETRIES = 5
BASE_DELAY = 1.0
MAX_DELAY = 30.0

# Injected so tests do not have to wait out a real backoff.
_sleep = asyncio.sleep


def _exception_types(*names: str) -> tuple[type, ...]:
    """The exception classes LiteLLM actually exposes, by name.

    Looked up rather than imported: the set moves between LiteLLM versions, and a missing name
    should cost one classification rather than breaking every model call at import time.
    """
    found = (getattr(litellm, name, None) for name in names)
    return tuple(entry for entry in found if isinstance(entry, type))


# Worth trying again: the request was fine and the far end was busy, slow, or briefly broken.
TRANSIENT_ERRORS = _exception_types(
    "RateLimitError", "Timeout", "APIConnectionError", "APIConnectionResetError",
    "InternalServerError", "ServiceUnavailableError",
)

# Never worth trying again: the request itself is the problem, so five more identical attempts
# cost five times as much, take half a minute, and end at the same error — while burying the
# one message that would have explained it.
PERMANENT_ERRORS = _exception_types(
    "AuthenticationError", "BadRequestError", "ContextWindowExceededError",
    "ContentPolicyViolationError", "NotFoundError", "PermissionDeniedError",
    "UnprocessableEntityError", "UnsupportedParamsError",
)


def is_retryable(exc: BaseException) -> bool:
    """Whether trying the same request again could plausibly succeed.

    Permanent classes are checked first and deliberately: LiteLLM's `BadRequestError` is not a
    subclass of `litellm.APIError` — the `APIError` in its ancestry is OpenAI's — so leaning on
    the class tree alone misclassifies. The HTTP status is the more reliable signal, and the
    class lists are there for the errors that never carry one.
    """
    if PERMANENT_ERRORS and isinstance(exc, PERMANENT_ERRORS):
        return False

    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        # 408 timeout, 409 conflict, 429 rate limit, and anything the server blames on itself.
        return status in {408, 409, 429} or status >= 500

    if TRANSIENT_ERRORS and isinstance(exc, TRANSIENT_ERRORS):
        return True

    # An unclassified failure is as likely to be a bug on this side as a blip on the other, and
    # retrying our own TypeError five times helps nobody.
    return False


def retry_after(exc: BaseException) -> float | None:
    """The provider's own `Retry-After`, in seconds, if it sent one.

    It knows when its rate limit resets and we do not, so its number beats our arithmetic —
    capped, because a mistaken or hostile header should not park a run for an hour.
    """
    for holder in (getattr(exc, "response", None), exc):
        headers = getattr(holder, "headers", None)
        if not headers:
            continue
        try:
            value = headers.get("retry-after") or headers.get("Retry-After")
        except AttributeError:
            continue
        if value is None:
            continue
        try:
            return min(float(value), MAX_DELAY)
        except (TypeError, ValueError):
            continue
    return None


def backoff_delay(attempt: int, exc: BaseException | None = None) -> float:
    """How long to wait before attempt number `attempt + 1`.

    Exponential, and jittered to somewhere between half and all of the nominal delay. The
    jitter matters here more than usual: agents fan out in parallel, so a shared rate limit
    hits several at the same instant and un-jittered backoff would march them back in step.
    """
    if exc is not None:
        stated = retry_after(exc)
        if stated is not None:
            return stated

    nominal = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
    return nominal * (0.5 + random.random() * 0.5)


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
        max_retries: int = MAX_RETRIES,
    ) -> Completion:
        """Run one model turn.

        A transient failure — a rate limit, a timeout, a provider having a moment — is retried
        up to `max_retries` times with a growing, jittered backoff. A failure caused by the
        request itself is raised at once, because repeating it only delays the explanation.

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

        if on_text is not None:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}

        return await cls._call_with_retries(kwargs, on_text, max_retries)

    @classmethod
    async def _call_with_retries(
        cls,
        kwargs: dict[str, Any],
        on_text: TextCallback | None,
        max_retries: int,
    ) -> Completion:
        """One model turn, tried again when the failure looks like it might pass.

        A single rate limit used to end a whole agent run — and an agent that takes a hundred
        turns will meet one. Retrying here rather than in each caller covers the agent loop and
        the orchestrator from one place.
        """
        for attempt in range(1, max_retries + 2):
            # Whether the caller has already been handed part of an answer. Retrying after that
            # would replay the response from the beginning and print it twice, so a stream that
            # fails mid-flight is reported rather than repeated.
            emitted = False

            async def relay(text: str) -> None:
                nonlocal emitted
                emitted = True
                await on_text(text)  # type: ignore[misc]

            try:
                if on_text is None:
                    return cls._parse(await litellm.acompletion(**kwargs))
                stream = await litellm.acompletion(**kwargs)
                return await cls._consume_stream(stream, kwargs["messages"], relay)
            except Exception as exc:
                last = attempt > max_retries
                if last or not is_retryable(exc):
                    raise
                if emitted:
                    logger.warning(
                        "%s failed after streaming part of an answer, so it will not be "
                        "retried (that would repeat what you have already seen): %s",
                        kwargs.get("model"),
                        exc,
                    )
                    raise

                delay = backoff_delay(attempt, exc)
                logger.warning(
                    "%s attempt %d/%d failed (%s: %s); retrying in %.1fs",
                    kwargs.get("model"),
                    attempt,
                    max_retries + 1,
                    type(exc).__name__,
                    exc,
                    delay,
                )
                await _sleep(delay)

        # Unreachable: the final attempt either returns or raises.
        raise RuntimeError("retry loop ended without a result")  # pragma: no cover

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
