from __future__ import annotations

import warnings
from typing import Any, Protocol

from pydantic import BaseModel


# Per-token API prices as (input_per_1k_usd, output_per_1k_usd).
#
# Verify against the source pages before editing — prices change and models get
# delisted. Numbers no longer quoted on an official page are dropped rather than
# kept on stale figures.
#   OpenAI:    https://developers.openai.com/api/docs/pricing
#   Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
# As of 2026-07-11. The official pages quote USD per 1M tokens; the values below
# are per 1K (divide by 1000). Keys are model-id families; dated snapshots such
# as "gpt-5.4-nano-2026-03-17" resolve to their family price via the
# longest-prefix fallback in ``_resolve_pricing``. Models absent from the table
# warn once and are costed at $0 (see ``estimate_usd``).
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI — https://developers.openai.com/api/docs/pricing
    "gpt-5.6-sol": (0.005, 0.030),
    "gpt-5.6-terra": (0.0025, 0.015),
    "gpt-5.6-luna": (0.001, 0.006),
    "gpt-5.5-pro": (0.030, 0.180),
    "gpt-5.5": (0.005, 0.030),
    "gpt-5.4": (0.0025, 0.015),
    "gpt-5.4-mini": (0.00075, 0.0045),
    "gpt-5.4-nano": (0.0002, 0.00125),
    # Anthropic — https://platform.claude.com/docs/en/about-claude/pricing
    "claude-opus-4-8": (0.005, 0.025),
    "claude-opus-4-7": (0.005, 0.025),
    # Sonnet 5 introductory pricing through 2026-08-31; standard rate of
    # (0.003, 0.015) takes effect 2026-09-01 — refresh this entry then.
    "claude-sonnet-5": (0.002, 0.010),
    "claude-sonnet-4-6": (0.003, 0.015),
    "claude-sonnet-4-20250514": (0.003, 0.015),
    "claude-haiku-4-5": (0.001, 0.005),
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

    def restore(self) -> None:
        ...

    def last_usage(self) -> TokenUsage | None:
        ...

    def reset(self) -> None:
        ...


class NullCostTracker:
    def wrap(self, generator: Any) -> Any:
        return generator

    def restore(self) -> None:
        pass

    def last_usage(self) -> TokenUsage | None:
        return None

    def reset(self) -> None:
        pass


class _ResponseCapture:
    def __init__(self, inner: Any, sink: list) -> None:
        self._inner = inner
        self._sink = sink

    @property
    def __querypilot_inner__(self) -> Any:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def create(self, *args: Any, **kwargs: Any) -> Any:
        response = self._inner.create(*args, **kwargs)
        self._sink.append(response)
        return response


def _unwrap(namespace: Any) -> Any:
    while isinstance(namespace, _ResponseCapture):
        namespace = namespace.__querypilot_inner__
    return namespace


class OpenAICostTracker:
    def __init__(self) -> None:
        self._captured: list[Any] = []
        self._model: str | None = None
        self._wrapped: tuple[Any, Any] | None = None  # (client, original_responses)

    def wrap(self, generator: Any) -> Any:
        self._model = getattr(generator, "model", None)
        client = getattr(generator, "client", None)
        if client is None or not hasattr(client, "responses"):
            return generator
        original = _unwrap(client.responses)
        if isinstance(client.responses, _ResponseCapture):
            return generator  # already wrapped
        client.responses = _ResponseCapture(original, self._captured)
        self._wrapped = (client, original)
        return generator

    def restore(self) -> None:
        if self._wrapped is None:
            return
        client, original = self._wrapped
        client.responses = original
        self._wrapped = None

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


class LocalCostTracker:
    """Cost tracker for OpenAI-compatible local endpoints (Ollama, vLLM, ...).

    Wraps the Chat Completions surface to capture token usage when the server
    reports it, but always reports ``$0`` — local inference is free, so a dollar
    estimate would be noise (and would be *wrong* if the served model name
    happened to collide with a hosted-pricing entry in ``MODEL_PRICING``).
    """

    def __init__(self) -> None:
        self._captured: list[Any] = []
        self._model: str | None = None
        self._wrapped: tuple[Any, Any] | None = None  # (chat, original_completions)

    def wrap(self, generator: Any) -> Any:
        self._model = getattr(generator, "model", None)
        client = getattr(generator, "client", None)
        chat = getattr(client, "chat", None)
        if chat is None or not hasattr(chat, "completions"):
            return generator
        original = _unwrap(chat.completions)
        if isinstance(chat.completions, _ResponseCapture):
            return generator  # already wrapped
        chat.completions = _ResponseCapture(original, self._captured)
        self._wrapped = (chat, original)
        return generator

    def restore(self) -> None:
        if self._wrapped is None:
            return
        chat, original = self._wrapped
        chat.completions = original
        self._wrapped = None

    def last_usage(self) -> TokenUsage | None:
        if not self._captured:
            return None
        response = self._captured[-1]
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        prompt = _coerce_int(getattr(usage, "prompt_tokens", None))
        completion = _coerce_int(getattr(usage, "completion_tokens", None))
        if prompt is None and completion is None:
            return None
        prompt = prompt or 0
        completion = completion or 0
        return TokenUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            model=self._model,
            estimated_usd=0.0,  # local inference is free
        )

    def reset(self) -> None:
        self._captured.clear()


class AnthropicCostTracker:
    def __init__(self) -> None:
        self._captured: list[Any] = []
        self._model: str | None = None
        self._wrapped: tuple[Any, Any] | None = None  # (client, original_messages)

    def wrap(self, generator: Any) -> Any:
        self._model = getattr(generator, "model", None)
        client = getattr(generator, "client", None)
        if client is None or not hasattr(client, "messages"):
            return generator
        original = _unwrap(client.messages)
        if isinstance(client.messages, _ResponseCapture):
            return generator  # already wrapped
        client.messages = _ResponseCapture(original, self._captured)
        self._wrapped = (client, original)
        return generator

    def restore(self) -> None:
        if self._wrapped is None:
            return
        client, original = self._wrapped
        client.messages = original
        self._wrapped = None

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


# Unknown-model names already warned about, so the warning fires once per model
# rather than once per priced request.
_WARNED_UNKNOWN_MODELS: set[str] = set()


def _resolve_pricing(model: str) -> tuple[float, float] | None:
    """Resolve a model id to ``(input_per_1k, output_per_1k)``.

    Exact table entries win. Otherwise fall back to the longest family key that
    ``model`` extends on a hyphen boundary, so dated snapshots like
    ``gpt-5.4-nano-2026-03-17`` inherit their family price without a dedicated
    entry. Matching on the boundary keeps ``gpt-5.4`` from swallowing a
    hypothetical ``gpt-5.45``.
    """
    exact = MODEL_PRICING.get(model)
    if exact is not None:
        return exact
    best: tuple[float, float] | None = None
    best_len = -1
    for key, price in MODEL_PRICING.items():
        if model.startswith(f"{key}-") and len(key) > best_len:
            best = price
            best_len = len(key)
    return best


def _warn_unknown_model(model: str) -> None:
    """Warn (once per model) that a model has no pricing entry.

    A real eval run silently reported ``$0.0000`` for a model missing from
    ``MODEL_PRICING``; the warning keeps cost-per-query honest while callers
    still get a number back so reports don't crash. ``LocalCostTracker`` and
    ``NullCostTracker`` never reach here — they report $0 by design.
    """
    if model in _WARNED_UNKNOWN_MODELS:
        return
    _WARNED_UNKNOWN_MODELS.add(model)
    warnings.warn(
        f"No pricing entry for model {model!r}; reporting $0.00 cost. "
        f"Add it to MODEL_PRICING in querypilot.evals.cost for an accurate estimate.",
        stacklevel=3,
    )


def estimate_usd(model: str | None, prompt_tokens: int, completion_tokens: int) -> float | None:
    if model is None:
        return None
    pricing = _resolve_pricing(model)
    if pricing is None:
        _warn_unknown_model(model)
        return 0.0
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
