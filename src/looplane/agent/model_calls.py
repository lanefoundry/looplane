"""Native model waits, bounded retries, fallback, and usage/cache accounting."""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from looplane.agent.ports import EventEmitter, RemainingTime
from looplane.agent.state import TurnState
from looplane.cache_strategy import ProviderCacheTrace
from looplane.contracts import (
    ConversationItem,
    CostBreakdown,
    ModelTurn,
    ModelUsageRecord,
    ToolDefinition,
    Usage,
)
from looplane.models import ModelProvider, ProviderError
from looplane.provider_catalog import estimate_cost

MODEL_ATTEMPTS = 5
RETRY_BACKOFF_BASE_SECONDS = 1.0
RETRY_MAX_DELAY_SECONDS = 30.0
RETRY_SERVER_HINT_MAX_SECONDS = 300.0
RETRY_JITTER_FRACTION = 0.15


@dataclass
class ModelCallState:
    candidates: tuple[ModelProvider, ...]
    active_index: int = 0
    provider_failure_codes: list[int | None] = field(default_factory=list)

    @property
    def model(self) -> ModelProvider:
        return self.candidates[self.active_index]


def retry_delay_seconds(attempt: int, retry_after_seconds: float | None) -> float:
    """Exponential backoff with ±15% jitter; a server Retry-After hint wins verbatim.

    The hint bypasses the local backoff curve but is capped for safety, mirroring
    how Claude Code treats the header as a server directive above local policy.
    """

    if retry_after_seconds is not None:
        return min(max(retry_after_seconds, 0.0), RETRY_SERVER_HINT_MAX_SECONDS)
    base = min(RETRY_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), RETRY_MAX_DELAY_SECONDS)
    return base * random.uniform(1.0 - RETRY_JITTER_FRACTION, 1.0 + RETRY_JITTER_FRACTION)


def add_usage(left: Usage, right: Usage) -> Usage:
    return Usage(
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        cached_input_tokens=left.cached_input_tokens + right.cached_input_tokens,
        reasoning_tokens=left.reasoning_tokens + right.reasoning_tokens,
        provider_total_tokens=left.total_tokens + right.total_tokens,
    )


def record_model_usage(state: TurnState, lane: str, model: ModelProvider, usage: Usage) -> None:
    state.usage = add_usage(state.usage, usage)
    state.model_usage.append(
        ModelUsageRecord(
            lane=lane,
            provider=model.provider_name,
            model=model.model_id,
            usage=usage,
            cost=estimate_cost(model.provider_name, model.model_id, usage),
        )
    )


async def record_provider_cache_trace(
    lane: str, model: ModelProvider, *, step: int, run_dir: Path, emit: EventEmitter
) -> None:
    trace = getattr(model, "last_cache_trace", None)
    if not isinstance(trace, ProviderCacheTrace):
        return
    payload = {
        "step": step,
        "lane": lane,
        "provider": model.provider_name,
        "model": model.model_id,
        "trace": {
            "provider": trace.provider,
            "prompt_cache_key": trace.prompt_cache_key,
            "tool_schema_fingerprint": trace.tool_schema_fingerprint,
            "cache_control_blocks": trace.cache_control_blocks,
            "warnings": list(trace.warnings),
            "cache_ready": trace.cache_ready,
        },
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "cache-traces.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    await emit(
        "model.cache_trace",
        step=step,
        lane=lane,
        provider=model.provider_name,
        model=model.model_id,
        cache_ready=trace.cache_ready,
        prompt_cache_key=trace.prompt_cache_key,
        tool_schema_fingerprint=trace.tool_schema_fingerprint,
        cache_control_blocks=trace.cache_control_blocks,
        warnings=list(trace.warnings),
    )


def aggregate_cost(state: TurnState, model: ModelProvider) -> CostBreakdown | None:
    if not state.model_usage:
        return estimate_cost(model.provider_name, model.model_id, state.usage)
    providers = {(record.provider, record.model) for record in state.model_usage}
    if len(providers) != 1:
        return None
    provider, model = next(iter(providers))
    return estimate_cost(provider, model, state.usage)


async def complete_model_or_cancel(
    model: ModelProvider,
    messages: Sequence[ConversationItem],
    tools: tuple[ToolDefinition, ...],
    cancel_requested: asyncio.Event,
    remaining: float,
) -> ModelTurn | None:
    """Cancel a pure model wait immediately without interrupting side-effecting tools."""

    model_task = asyncio.create_task(model.complete(messages, tools))
    cancel_task = asyncio.create_task(cancel_requested.wait())
    try:
        done, _ = await asyncio.wait(
            (model_task, cancel_task),
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            model_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await model_task
            raise TimeoutError("model request exceeded remaining wall time")
        if cancel_task in done and cancel_requested.is_set():
            model_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await model_task
            return None
        return await model_task
    finally:
        if not model_task.done():
            model_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await model_task
        cancel_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_task


async def complete_model_wind_down(
    model: ModelProvider,
    messages: Sequence[ConversationItem],
    cancel_requested: asyncio.Event,
    remaining: float,
) -> ModelTurn | None:
    """One toolless model call for the wind-down summary.

    Similar to ``_complete_model_or_cancel`` but passes an empty tools
    list so the model can only produce a text response.  Retries once on
    transient errors; anything else is silently swallowed by the caller.
    """

    model_task = asyncio.create_task(model.complete(messages, tools=()))
    cancel_task = asyncio.create_task(cancel_requested.wait())
    try:
        done, _ = await asyncio.wait(
            (model_task, cancel_task),
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            model_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await model_task
            return None
        if cancel_task in done and cancel_requested.is_set():
            model_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await model_task
            return None
        return await model_task
    finally:
        if not model_task.done():
            model_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await model_task
        cancel_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_task


async def backoff_sleep(cancel_requested: asyncio.Event, delay: float) -> None:
    """Wait out the retry backoff; user cancellation ends the wait early."""

    wake = asyncio.create_task(cancel_requested.wait())
    try:
        await asyncio.wait((wake,), timeout=delay)
    finally:
        wake.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wake


async def complete_model_with_retry(
    models: ModelCallState,
    state: TurnState,
    *,
    tool_definitions: Callable[[], tuple[ToolDefinition, ...]],
    cancel_requested: asyncio.Event,
    remaining: RemainingTime,
    deadline: float,
    emit: EventEmitter,
    retry_delay: Callable[[int, float | None], float],
) -> ModelTurn | None:
    """One logical model step, retrying transient provider failures in place.

    Retryable errors (server 5xx, rate limits, transport drops) are retried up
    to ``MODEL_ATTEMPTS`` times with jittered exponential backoff; auth and
    invalid-request failures re-raise immediately. When a candidate exhausts
    its retry budget, the next fallback model (if any) takes over with a
    fresh budget. Cancellation during backoff shortens the wait, and the
    next attempt observes it immediately.
    """

    last_error: ProviderError | None = None
    for candidate_index, candidate in enumerate(models.candidates):
        models.active_index = candidate_index
        models.provider_failure_codes = []
        for attempt in range(1, MODEL_ATTEMPTS + 1):
            try:
                return await complete_model_or_cancel(
                    models.model,
                    state.messages,
                    tool_definitions(),
                    cancel_requested,
                    remaining(deadline),
                )
            except ProviderError as exc:
                if not exc.retryable:
                    raise
                last_error = exc
                models.provider_failure_codes.append(exc.status_code)
                if attempt == MODEL_ATTEMPTS:
                    break
                delay = retry_delay(attempt, exc.retry_after_seconds)
                await emit(
                    "model.retry",
                    attempt=attempt,
                    provider=exc.provider_name,
                    error=str(exc),
                    delay_seconds=delay,
                )
                await backoff_sleep(cancel_requested, delay)
        if candidate_index + 1 < len(models.candidates):
            successor = models.candidates[candidate_index + 1]
            await emit(
                "model.fallback",
                from_provider=candidate.provider_name,
                from_model=candidate.model_id,
                to_provider=successor.provider_name,
                to_model=successor.model_id,
                failure_codes=list(models.provider_failure_codes),
            )
            continue
        assert last_error is not None
        raise last_error
    raise AssertionError("unreachable: retry loop must return or raise")
