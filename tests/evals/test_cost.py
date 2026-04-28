from __future__ import annotations

from typing import Any

import pytest

from querypilot.evals.cost import (
    MODEL_PRICING,
    AnthropicCostTracker,
    NullCostTracker,
    OpenAICostTracker,
    TokenUsage,
    estimate_usd,
)


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
    response = _FakeResponse(usage=_FakeUsage(input_tokens=120, output_tokens=80))
    generator = _FakeOpenAIGenerator(model="gpt-4o-mini", response=response)
    tracker = OpenAICostTracker()
    tracker.wrap(generator)

    generator.call_create()
    usage = tracker.last_usage()

    assert usage == TokenUsage(
        prompt_tokens=120,
        completion_tokens=80,
        total_tokens=200,
        model="gpt-4o-mini",
        estimated_usd=round(120 / 1000 * 0.00015 + 80 / 1000 * 0.0006, 6),
    )


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
    usage = tracker.last_usage()

    assert usage is not None
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 5
    assert usage.estimated_usd is None


def test_openai_tracker_reset_clears_history() -> None:
    response = _FakeResponse(usage=_FakeUsage(input_tokens=1, output_tokens=1))
    generator = _FakeOpenAIGenerator(model="gpt-4o-mini", response=response)
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

    generator = _MultiGen(model="gpt-4o-mini", responses=[first, second])
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


def test_estimate_usd_returns_none_for_unknown_model() -> None:
    assert estimate_usd("nonexistent-model", 1000, 500) is None


def test_estimate_usd_returns_none_when_model_is_none() -> None:
    assert estimate_usd(None, 1000, 500) is None


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
