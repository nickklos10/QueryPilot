from __future__ import annotations

from typing import Any

import pytest

import querypilot.evals.cost as cost
from querypilot.evals.cost import (
    MODEL_PRICING,
    AnthropicCostTracker,
    LocalCostTracker,
    NullCostTracker,
    OpenAICostTracker,
    TokenUsage,
    estimate_usd,
)


@pytest.fixture(autouse=True)
def _reset_unknown_model_warnings() -> None:
    # estimate_usd warns once per unknown model for the life of the process;
    # clear the dedup set so each test observes warnings independently.
    cost._WARNED_UNKNOWN_MODELS.clear()


class _FakeUsage:
    def __init__(self, input_tokens: int | None = None, output_tokens: int | None = None) -> None:
        if input_tokens is not None:
            self.input_tokens = input_tokens
        if output_tokens is not None:
            self.output_tokens = output_tokens


class _FakeResponse:
    def __init__(self, usage: Any | None = None) -> None:
        if usage is not None:
            self.usage = usage


class _FakeResponses:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[tuple] = []

    def create(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        self.calls.append((args, kwargs))
        return self._response


class _FakeOpenAIClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.responses = _FakeResponses(response)


class _FakeOpenAIGenerator:
    def __init__(self, model: str, response: _FakeResponse) -> None:
        self.client = _FakeOpenAIClient(response)
        self.model = model

    def call_create(self) -> _FakeResponse:
        return self.client.responses.create(model=self.model)


class _FakeMessages:
    def __init__(self, message: _FakeResponse) -> None:
        self._message = message

    def create(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        return self._message


class _FakeAnthropicClient:
    def __init__(self, message: _FakeResponse) -> None:
        self.messages = _FakeMessages(message)


class _FakeAnthropicGenerator:
    def __init__(self, model: str, message: _FakeResponse) -> None:
        self.client = _FakeAnthropicClient(message)
        self.model = model

    def call_create(self) -> _FakeResponse:
        return self.client.messages.create(model=self.model)


class _FakeChatUsage:
    def __init__(
        self, prompt_tokens: int | None = None, completion_tokens: int | None = None
    ) -> None:
        if prompt_tokens is not None:
            self.prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            self.completion_tokens = completion_tokens


class _FakeChatCompletions:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[tuple] = []

    def create(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        self.calls.append((args, kwargs))
        return self._response


class _FakeChat:
    def __init__(self, response: _FakeResponse) -> None:
        self.completions = _FakeChatCompletions(response)


class _FakeLocalClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.chat = _FakeChat(response)


class _FakeLocalGenerator:
    def __init__(self, model: str, response: _FakeResponse) -> None:
        self.client = _FakeLocalClient(response)
        self.model = model

    def call_create(self) -> _FakeResponse:
        return self.client.chat.completions.create(model=self.model)


def test_local_tracker_extracts_usage_and_reports_zero_cost() -> None:
    response = _FakeResponse(usage=_FakeChatUsage(prompt_tokens=200, completion_tokens=60))
    generator = _FakeLocalGenerator(model="llama3.1", response=response)
    tracker = LocalCostTracker()
    tracker.wrap(generator)

    generator.call_create()
    usage = tracker.last_usage()

    assert usage == TokenUsage(
        prompt_tokens=200,
        completion_tokens=60,
        total_tokens=260,
        model="llama3.1",
        estimated_usd=0.0,
    )


def test_local_tracker_reports_zero_cost_even_on_pricing_collision(
    recwarn: pytest.WarningsRecorder,
) -> None:
    # A local model named like a hosted one must still cost $0 (no MODEL_PRICING
    # lookup) and must not emit an unknown-model warning.
    response = _FakeResponse(usage=_FakeChatUsage(prompt_tokens=1000, completion_tokens=1000))
    generator = _FakeLocalGenerator(model="gpt-5.4", response=response)
    tracker = LocalCostTracker()
    tracker.wrap(generator)

    generator.call_create()
    usage = tracker.last_usage()

    assert usage is not None
    assert usage.estimated_usd == 0.0
    assert not [w for w in recwarn if "pricing entry" in str(w.message)]


def test_local_tracker_returns_none_before_any_call() -> None:
    response = _FakeResponse(usage=_FakeChatUsage(prompt_tokens=1, completion_tokens=1))
    generator = _FakeLocalGenerator(model="llama3.1", response=response)
    tracker = LocalCostTracker()
    tracker.wrap(generator)

    assert tracker.last_usage() is None


def test_local_tracker_passes_through_when_response_has_no_usage() -> None:
    response = _FakeResponse()
    generator = _FakeLocalGenerator(model="llama3.1", response=response)
    tracker = LocalCostTracker()
    tracker.wrap(generator)

    generator.call_create()

    assert tracker.last_usage() is None


def test_local_tracker_restore_returns_original_completions() -> None:
    response = _FakeResponse(usage=_FakeChatUsage(prompt_tokens=1, completion_tokens=1))
    generator = _FakeLocalGenerator(model="llama3.1", response=response)
    original = generator.client.chat.completions
    tracker = LocalCostTracker()

    tracker.wrap(generator)
    assert generator.client.chat.completions is not original

    tracker.restore()
    assert generator.client.chat.completions is original


def test_local_tracker_returns_generator_when_chat_missing() -> None:
    class _BareGen:
        model = "llama3.1"

    bare = _BareGen()
    tracker = LocalCostTracker()

    result = tracker.wrap(bare)

    assert result is bare
    assert tracker.last_usage() is None


def test_null_cost_tracker_returns_none() -> None:
    tracker = NullCostTracker()
    wrapped = tracker.wrap(object())

    assert tracker.last_usage() is None
    assert wrapped is not None


def test_null_cost_tracker_wrap_returns_same_generator() -> None:
    tracker = NullCostTracker()
    sentinel = object()

    assert tracker.wrap(sentinel) is sentinel


def test_openai_tracker_extracts_usage_after_call() -> None:
    # Token counts mirror the real gpt-5.4-nano run that reported $0 before the
    # 2026 lineup was priced.
    response = _FakeResponse(usage=_FakeUsage(input_tokens=841, output_tokens=263))
    generator = _FakeOpenAIGenerator(model="gpt-5.4-nano", response=response)
    tracker = OpenAICostTracker()
    tracker.wrap(generator)

    generator.call_create()
    usage = tracker.last_usage()

    assert usage == TokenUsage(
        prompt_tokens=841,
        completion_tokens=263,
        total_tokens=1104,
        model="gpt-5.4-nano",
        estimated_usd=round(841 / 1000 * 0.0002 + 263 / 1000 * 0.00125, 6),
    )
    assert usage.estimated_usd is not None and usage.estimated_usd > 0


def test_openai_tracker_returns_none_before_any_call() -> None:
    response = _FakeResponse(usage=_FakeUsage(input_tokens=120, output_tokens=80))
    generator = _FakeOpenAIGenerator(model="gpt-4o-mini", response=response)
    tracker = OpenAICostTracker()
    tracker.wrap(generator)

    assert tracker.last_usage() is None


def test_openai_tracker_passes_through_when_response_has_no_usage() -> None:
    response = _FakeResponse()
    generator = _FakeOpenAIGenerator(model="gpt-4o-mini", response=response)
    tracker = OpenAICostTracker()
    tracker.wrap(generator)

    generator.call_create()

    assert tracker.last_usage() is None


def test_openai_tracker_handles_unknown_model_with_no_pricing() -> None:
    response = _FakeResponse(usage=_FakeUsage(input_tokens=10, output_tokens=5))
    generator = _FakeOpenAIGenerator(model="custom-fine-tune", response=response)
    tracker = OpenAICostTracker()
    tracker.wrap(generator)

    generator.call_create()
    with pytest.warns(UserWarning, match="custom-fine-tune"):
        usage = tracker.last_usage()

    assert usage is not None
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 5
    # Token counts are still reported; the dollar figure falls back to $0 loudly.
    assert usage.estimated_usd == 0.0


def test_openai_tracker_reset_clears_history() -> None:
    response = _FakeResponse(usage=_FakeUsage(input_tokens=1, output_tokens=1))
    generator = _FakeOpenAIGenerator(model="gpt-5.4-nano", response=response)
    tracker = OpenAICostTracker()
    tracker.wrap(generator)

    generator.call_create()
    assert tracker.last_usage() is not None

    tracker.reset()
    assert tracker.last_usage() is None


def test_openai_tracker_keeps_only_last_response() -> None:
    first = _FakeResponse(usage=_FakeUsage(input_tokens=10, output_tokens=10))
    second = _FakeResponse(usage=_FakeUsage(input_tokens=99, output_tokens=99))

    class _MultiResponses:
        def __init__(self, responses: list[_FakeResponse]) -> None:
            self._iter = iter(responses)

        def create(self, *args: Any, **kwargs: Any) -> _FakeResponse:
            return next(self._iter)

    class _MultiClient:
        def __init__(self, responses: list[_FakeResponse]) -> None:
            self.responses = _MultiResponses(responses)

    class _MultiGen:
        def __init__(self, model: str, responses: list[_FakeResponse]) -> None:
            self.client = _MultiClient(responses)
            self.model = model

    generator = _MultiGen(model="gpt-5.4-nano", responses=[first, second])
    tracker = OpenAICostTracker()
    tracker.wrap(generator)

    generator.client.responses.create()
    generator.client.responses.create()

    usage = tracker.last_usage()
    assert usage is not None
    assert usage.prompt_tokens == 99
    assert usage.completion_tokens == 99


def test_anthropic_tracker_extracts_usage_after_call() -> None:
    message = _FakeResponse(usage=_FakeUsage(input_tokens=300, output_tokens=120))
    generator = _FakeAnthropicGenerator(model="claude-sonnet-4-6", message=message)
    tracker = AnthropicCostTracker()
    tracker.wrap(generator)

    generator.call_create()
    usage = tracker.last_usage()

    assert usage == TokenUsage(
        prompt_tokens=300,
        completion_tokens=120,
        total_tokens=420,
        model="claude-sonnet-4-6",
        estimated_usd=round(300 / 1000 * 0.003 + 120 / 1000 * 0.015, 6),
    )


def test_anthropic_tracker_returns_none_for_missing_usage() -> None:
    message = _FakeResponse()
    generator = _FakeAnthropicGenerator(model="claude-sonnet-4-6", message=message)
    tracker = AnthropicCostTracker()
    tracker.wrap(generator)

    generator.call_create()

    assert tracker.last_usage() is None


def test_estimate_usd_warns_and_returns_zero_for_unknown_model() -> None:
    with pytest.warns(UserWarning, match="nonexistent-model"):
        cost_usd = estimate_usd("nonexistent-model", 1000, 500)

    assert cost_usd == 0.0


def test_estimate_usd_warns_once_per_unknown_model() -> None:
    with pytest.warns(UserWarning) as records:
        first = estimate_usd("mystery-model", 1000, 500)
        second = estimate_usd("mystery-model", 999, 111)

    assert first == 0.0
    assert second == 0.0
    assert len(records) == 1  # deduped by model name, not per call


def test_estimate_usd_returns_none_when_model_is_none() -> None:
    assert estimate_usd(None, 1000, 500) is None


def test_estimate_usd_openai_new_model_arithmetic() -> None:
    # gpt-5.4-nano: $0.20 / $1.25 per 1M tokens -> 0.0002 / 0.00125 per 1K.
    cost_usd = estimate_usd("gpt-5.4-nano", 841, 263)

    assert cost_usd == round(841 / 1000 * 0.0002 + 263 / 1000 * 0.00125, 6)
    assert cost_usd == pytest.approx(0.000497)


def test_estimate_usd_anthropic_new_model_arithmetic() -> None:
    # claude-opus-4-8: $5 / $25 per 1M tokens -> 0.005 / 0.025 per 1K.
    cost_usd = estimate_usd("claude-opus-4-8", 2000, 500)

    assert cost_usd == round(2000 / 1000 * 0.005 + 500 / 1000 * 0.025, 6)
    assert cost_usd == pytest.approx(0.0225)


def test_estimate_usd_resolves_dated_openai_snapshot_to_family() -> None:
    dated = estimate_usd("gpt-5.4-nano-2026-03-17", 1000, 1000)
    family = estimate_usd("gpt-5.4-nano", 1000, 1000)

    assert dated is not None
    assert dated == family
    assert dated > 0


def test_estimate_usd_resolves_dated_anthropic_snapshot_to_family() -> None:
    dated = estimate_usd("claude-opus-4-8-20260528", 1000, 1000)
    family = estimate_usd("claude-opus-4-8", 1000, 1000)

    assert dated is not None
    assert dated == family
    assert dated > 0


def test_estimate_usd_prefix_fallback_prefers_longest_family() -> None:
    # A dated gpt-5.4-mini snapshot must resolve to gpt-5.4-mini, not gpt-5.4.
    resolved = estimate_usd("gpt-5.4-mini-2026-01-01", 1000, 1000)
    mini = estimate_usd("gpt-5.4-mini", 1000, 1000)
    base = estimate_usd("gpt-5.4", 1000, 1000)

    assert resolved == mini
    assert resolved != base


@pytest.mark.parametrize("model", list(MODEL_PRICING.keys()))
def test_estimate_usd_known_model_produces_positive_cost(model: str) -> None:
    cost = estimate_usd(model, 1000, 500)

    assert cost is not None
    assert cost > 0


def test_token_usage_serializable() -> None:
    usage = TokenUsage(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        model="gpt-4o-mini",
        estimated_usd=0.0042,
    )

    payload = usage.model_dump()

    assert payload["model"] == "gpt-4o-mini"
    assert payload["estimated_usd"] == 0.0042


def test_openai_tracker_returns_generator_when_client_missing() -> None:
    class _BareGen:
        model = "gpt-4o-mini"

    bare = _BareGen()
    tracker = OpenAICostTracker()

    result = tracker.wrap(bare)

    assert result is bare
    assert tracker.last_usage() is None


def test_openai_tracker_restore_returns_original_responses() -> None:
    response = _FakeResponse(usage=_FakeUsage(input_tokens=10, output_tokens=5))
    generator = _FakeOpenAIGenerator(model="gpt-4o-mini", response=response)
    original_responses = generator.client.responses
    tracker = OpenAICostTracker()

    tracker.wrap(generator)
    assert generator.client.responses is not original_responses

    tracker.restore()
    assert generator.client.responses is original_responses


def test_openai_tracker_restore_is_idempotent() -> None:
    response = _FakeResponse(usage=_FakeUsage(input_tokens=10, output_tokens=5))
    generator = _FakeOpenAIGenerator(model="gpt-4o-mini", response=response)
    tracker = OpenAICostTracker()
    tracker.wrap(generator)

    tracker.restore()
    tracker.restore()  # second call is a no-op


def test_openai_tracker_wrap_is_idempotent() -> None:
    response = _FakeResponse(usage=_FakeUsage(input_tokens=10, output_tokens=5))
    generator = _FakeOpenAIGenerator(model="gpt-4o-mini", response=response)
    tracker = OpenAICostTracker()
    tracker.wrap(generator)
    proxy_after_first_wrap = generator.client.responses

    tracker.wrap(generator)

    # Second wrap should NOT nest a proxy on top of the existing proxy.
    assert generator.client.responses is proxy_after_first_wrap


def test_anthropic_tracker_restore_returns_original_messages() -> None:
    message = _FakeResponse(usage=_FakeUsage(input_tokens=10, output_tokens=5))
    generator = _FakeAnthropicGenerator(model="claude-sonnet-4-6", message=message)
    original_messages = generator.client.messages
    tracker = AnthropicCostTracker()

    tracker.wrap(generator)
    tracker.restore()

    assert generator.client.messages is original_messages


def test_null_cost_tracker_has_restore() -> None:
    tracker = NullCostTracker()
    tracker.restore()  # must not raise


def test_local_tracker_silent_for_unknown_model(recwarn: pytest.WarningsRecorder) -> None:
    # Local inference is free by design and never consults MODEL_PRICING, so an
    # unrecognized served model name must not trigger the unknown-model warning.
    response = _FakeResponse(usage=_FakeChatUsage(prompt_tokens=500, completion_tokens=250))
    generator = _FakeLocalGenerator(model="some-unlisted-local-model", response=response)
    tracker = LocalCostTracker()
    tracker.wrap(generator)

    generator.call_create()
    usage = tracker.last_usage()

    assert usage is not None
    assert usage.estimated_usd == 0.0
    assert not [w for w in recwarn if "pricing entry" in str(w.message)]


def test_null_cost_tracker_silent(recwarn: pytest.WarningsRecorder) -> None:
    tracker = NullCostTracker()
    tracker.wrap(object())

    assert tracker.last_usage() is None
    assert not [w for w in recwarn if "pricing entry" in str(w.message)]
