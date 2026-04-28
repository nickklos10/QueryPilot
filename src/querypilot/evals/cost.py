from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel


# (input_per_1k_usd, output_per_1k_usd). Missing models leave estimated_usd unset.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI Responses API (illustrative; refresh as pricing changes)
    "gpt-5.1": (0.01, 0.03),
    "gpt-4o": (0.005, 0.015),
    "gpt-4o-mini": (0.00015, 0.0006),
    # Anthropic Messages API
    "claude-opus-4-7": (0.015, 0.075),
    "claude-sonnet-4-6": (0.003, 0.015),
    "claude-sonnet-4-20250514": (0.003, 0.015),
    "claude-haiku-4-5-20251001": (0.00025, 0.00125),
}


class TokenUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str | None = None
    estimated_usd: float | None = None


class CostTracker(Protocol):
    def wrap(self, generator: Any) -> Any:
        ...

    def last_usage(self) -> TokenUsage | None:
        ...

    def reset(self) -> None:
        ...


class NullCostTracker:
    def wrap(self, generator: Any) -> Any:
        return generator

    def last_usage(self) -> TokenUsage | None:
        return None

    def reset(self) -> None:
        pass


class _ResponseCapture:
    def __init__(self, inner: Any, sink: list) -> None:
        self._inner = inner
        self._sink = sink

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def create(self, *args: Any, **kwargs: Any) -> Any:
        response = self._inner.create(*args, **kwargs)
        self._sink.append(response)
        return response


class OpenAICostTracker:
    def __init__(self) -> None:
        self._captured: list[Any] = []
        self._model: str | None = None

    def wrap(self, generator: Any) -> Any:
        self._model = getattr(generator, "model", None)
        client = getattr(generator, "client", None)
        if client is None or not hasattr(client, "responses"):
            return generator
        client.responses = _ResponseCapture(client.responses, self._captured)
        return generator

    def last_usage(self) -> TokenUsage | None:
        if not self._captured:
            return None
        response = self._captured[-1]
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        prompt = _coerce_int(getattr(usage, "input_tokens", None))
        completion = _coerce_int(getattr(usage, "output_tokens", None))
        if prompt is None and completion is None:
            return None
        prompt = prompt or 0
        completion = completion or 0
        return TokenUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            model=self._model,
            estimated_usd=estimate_usd(self._model, prompt, completion),
        )

    def reset(self) -> None:
        self._captured.clear()


class AnthropicCostTracker:
    def __init__(self) -> None:
        self._captured: list[Any] = []
        self._model: str | None = None

    def wrap(self, generator: Any) -> Any:
        self._model = getattr(generator, "model", None)
        client = getattr(generator, "client", None)
        if client is None or not hasattr(client, "messages"):
            return generator
        client.messages = _ResponseCapture(client.messages, self._captured)
        return generator

    def last_usage(self) -> TokenUsage | None:
        if not self._captured:
            return None
        message = self._captured[-1]
        usage = getattr(message, "usage", None)
        if usage is None:
            return None
        prompt = _coerce_int(getattr(usage, "input_tokens", None))
        completion = _coerce_int(getattr(usage, "output_tokens", None))
        if prompt is None and completion is None:
            return None
        prompt = prompt or 0
        completion = completion or 0
        return TokenUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            model=self._model,
            estimated_usd=estimate_usd(self._model, prompt, completion),
        )

    def reset(self) -> None:
        self._captured.clear()


def estimate_usd(model: str | None, prompt_tokens: int, completion_tokens: int) -> float | None:
    if model is None:
        return None
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return None
    input_per_1k, output_per_1k = pricing
    return round(
        (prompt_tokens / 1000.0) * input_per_1k
        + (completion_tokens / 1000.0) * output_per_1k,
        6,
    )


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
