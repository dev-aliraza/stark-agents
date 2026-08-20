"""Retrying a model call that failed for a reason that might pass.

A single rate limit used to end a whole agent run, and an agent that takes a hundred turns will
meet one. The interesting half is not the retrying — it is knowing what *not* to retry, since
repeating a malformed request five times costs five times as much, takes half a minute, and
ends at the same error with the explanation buried.
"""

from __future__ import annotations

import litellm
import pytest

from stark.llm import client as llm_client
from stark.llm.client import LLMClient, backoff_delay, is_retryable, retry_after
from stark.types import Completion


def error(name: str, **extra):
    """One of LiteLLM's exceptions, however this version wants to be constructed."""
    cls = getattr(litellm, name)
    for attempt in (
        lambda: cls(message="boom", llm_provider="anthropic", model="claude-opus-5", **extra),
        lambda: cls("boom", **extra),
        lambda: cls(message="boom", **extra),
    ):
        try:
            return attempt()
        except Exception:
            continue
    pytest.skip(f"cannot construct litellm.{name} in this version")


@pytest.fixture(autouse=True)
def no_real_waiting(monkeypatch):
    """Record the backoffs instead of sleeping through them."""
    waited: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(llm_client, "_sleep", fake_sleep)
    return waited


class Flaky:
    """Fails a set number of times, then succeeds."""

    def __init__(self, failures: int, exc):
        self.failures = failures
        self.exc = exc
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return _ok()


def _ok():
    """The shape `_parse` expects from a non-streamed response."""

    class Function:
        name = "look"
        arguments = "{}"

    class Raw:
        id = "c1"
        function = Function()

    class Message:
        content = "done"
        tool_calls = [Raw()]

    class Choice:
        finish_reason = "tool_calls"
        message = Message()

    class Response:
        choices = [Choice()]

    return Response()


# --- what is worth retrying ------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["RateLimitError", "Timeout", "APIConnectionError", "InternalServerError",
     "ServiceUnavailableError"],
)
def test_a_transient_failure_is_retried(name):
    assert is_retryable(error(name)) is True


@pytest.mark.parametrize(
    "name",
    ["AuthenticationError", "BadRequestError", "ContextWindowExceededError",
     "ContentPolicyViolationError", "NotFoundError", "PermissionDeniedError"],
)
def test_a_failure_caused_by_the_request_is_not(name):
    """Five more identical attempts end at the same error, half a minute later."""
    assert is_retryable(error(name)) is False


def test_our_own_bug_is_not_retried():
    """A TypeError here is a defect on this side, not a blip on the other."""
    assert is_retryable(TypeError("nope")) is False


def test_the_http_status_decides_when_the_class_does_not():
    """LiteLLM's BadRequestError is not a `litellm.APIError` subclass, so the tree misleads."""

    class Odd(Exception):
        status_code = 503

    class AlsoOdd(Exception):
        status_code = 422

    assert is_retryable(Odd()) is True
    assert is_retryable(AlsoOdd()) is False


# --- how long it waits ------------------------------------------------------------------------


def test_the_delay_grows():
    assert backoff_delay(1) < backoff_delay(4) < backoff_delay(6)


def test_the_delay_is_jittered():
    """Agents fan out in parallel, so a shared rate limit hits several at the same instant."""
    delays = {round(backoff_delay(3), 4) for _ in range(20)}
    assert len(delays) > 1, "un-jittered backoff would march parallel agents back in step"


def test_the_delay_is_capped():
    assert backoff_delay(50) <= llm_client.MAX_DELAY


def test_the_providers_own_retry_after_wins():
    """It knows when its limit resets; we are guessing."""

    class WithHeader(Exception):
        headers = {"retry-after": "7"}

    assert retry_after(WithHeader()) == 7
    assert backoff_delay(1, WithHeader()) == 7


def test_an_absurd_retry_after_is_capped():
    """A mistaken or hostile header must not park a run for an hour."""

    class Hostile(Exception):
        headers = {"retry-after": "999999"}

    assert retry_after(Hostile()) == llm_client.MAX_DELAY


def test_a_junk_retry_after_is_ignored():
    class Junk(Exception):
        headers = {"retry-after": "soon"}

    assert retry_after(Junk()) is None


# --- the loop ---------------------------------------------------------------------------------


async def call(monkeypatch, flaky, **kwargs):
    monkeypatch.setattr(litellm, "acompletion", flaky)
    return await LLMClient.complete(
        provider="anthropic", model="claude-opus-5", messages=[{"role": "user", "content": "go"}],
        **kwargs,
    )


async def test_it_recovers_from_a_transient_failure(monkeypatch, no_real_waiting):
    flaky = Flaky(failures=3, exc=error("RateLimitError"))
    result = await call(monkeypatch, flaky)

    assert result.content == "done"
    assert flaky.calls == 4
    assert len(no_real_waiting) == 3


async def test_it_gives_up_after_five_retries(monkeypatch, no_real_waiting):
    """Six attempts in total, then the real error — not a wrapper hiding it."""
    flaky = Flaky(failures=99, exc=error("RateLimitError"))

    with pytest.raises(Exception) as raised:
        await call(monkeypatch, flaky)

    assert flaky.calls == 6
    assert len(no_real_waiting) == 5
    assert isinstance(raised.value, litellm.RateLimitError)


async def test_a_permanent_failure_is_raised_at_once(monkeypatch, no_real_waiting):
    flaky = Flaky(failures=99, exc=error("BadRequestError"))

    with pytest.raises(Exception):
        await call(monkeypatch, flaky)

    assert flaky.calls == 1, "a bad request must not be sent six times"
    assert no_real_waiting == []


async def test_the_retry_budget_is_adjustable(monkeypatch, no_real_waiting):
    flaky = Flaky(failures=99, exc=error("RateLimitError"))

    with pytest.raises(Exception):
        await call(monkeypatch, flaky, max_retries=1)

    assert flaky.calls == 2


async def test_no_retries_means_one_attempt(monkeypatch, no_real_waiting):
    flaky = Flaky(failures=1, exc=error("RateLimitError"))

    with pytest.raises(Exception):
        await call(monkeypatch, flaky, max_retries=0)

    assert flaky.calls == 1


# --- streaming: a retry must not replay what the user already saw ------------------------------


class FlakyStream:
    """Yields some deltas, then fails — or succeeds outright once `fail_until` is past."""

    def __init__(self, fail_until: int, deltas_before_failure: int, exc):
        self.fail_until = fail_until
        self.deltas_before_failure = deltas_before_failure
        self.exc = exc
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        failing = self.calls <= self.fail_until
        deltas = self.deltas_before_failure if failing else 2

        async def stream():
            for index in range(deltas):
                yield _delta(f"part{index} ")
            if failing:
                raise self.exc

        return stream()


def _delta(text: str):
    class Delta:
        content = text
        tool_calls = None

    class Choice:
        delta = Delta()
        finish_reason = None

    class Chunk:
        choices = [Choice()]
        usage = None

    return Chunk()


async def stream_call(monkeypatch, flaky, seen, **kwargs):
    monkeypatch.setattr(litellm, "acompletion", flaky)

    async def on_text(text: str) -> None:
        seen.append(text)

    return await LLMClient.complete(
        provider="anthropic", model="claude-opus-5",
        messages=[{"role": "user", "content": "go"}], on_text=on_text, **kwargs,
    )


async def test_a_stream_that_failed_before_emitting_is_retried(monkeypatch, no_real_waiting):
    """Nothing reached the caller, so trying again is free of side effects."""
    seen: list[str] = []
    flaky = FlakyStream(fail_until=2, deltas_before_failure=0, exc=error("RateLimitError"))

    await stream_call(monkeypatch, flaky, seen)

    assert flaky.calls == 3
    assert "".join(seen) == "part0 part1 "


async def test_a_stream_that_failed_mid_answer_is_not_retried(monkeypatch, no_real_waiting):
    """Replaying it would print the beginning of the answer twice.

    The failure is surfaced instead — a visible error beats silently duplicated output that
    reads as the model repeating itself.
    """
    seen: list[str] = []
    flaky = FlakyStream(fail_until=99, deltas_before_failure=2, exc=error("RateLimitError"))

    with pytest.raises(Exception):
        await stream_call(monkeypatch, flaky, seen)

    assert flaky.calls == 1, "a partially streamed answer must not be replayed"
    assert no_real_waiting == []
    # What the caller saw is exactly what arrived once — not twice.
    assert seen == ["part0 ", "part1 "]


async def test_the_reason_for_not_retrying_a_stream_is_logged(monkeypatch, no_real_waiting, caplog):
    seen: list[str] = []
    flaky = FlakyStream(fail_until=99, deltas_before_failure=1, exc=error("RateLimitError"))

    with caplog.at_level("WARNING"):
        with pytest.raises(Exception):
            await stream_call(monkeypatch, flaky, seen)

    assert "will not be retried" in caplog.text


async def test_each_retry_is_logged_with_its_wait(monkeypatch, no_real_waiting, caplog):
    """A run that pauses for sixteen seconds should say why."""
    flaky = Flaky(failures=2, exc=error("RateLimitError"))

    with caplog.at_level("WARNING"):
        await call(monkeypatch, flaky)

    assert caplog.text.count("retrying in") == 2
    assert "RateLimitError" in caplog.text
