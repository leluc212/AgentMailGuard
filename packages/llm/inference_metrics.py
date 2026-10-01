"""Per-inference telemetry: context size, tokens, cost and one structured log line.

Requirements: R11.7 (record the final context token count on every inference request),
R21.4 (input/output token and cost counters), R21.6 (cost from the price table),
R21.3 (structured logs). Proposal line 948: context length is recorded on every request so it
can be correlated with quality, latency and cost.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any

from packages.core.pricing import estimate_inference_cost
from packages.core.settings import ModelPricing
from packages.knowledge.token_counter import TokenCounter
from packages.llm.protocol import (
    CallProvenance,
    ChatMessage,
    LLMProvider,
    LLMResult,
    ModelTier,
)
from packages.observability.metrics import record_ai_cost

logger = logging.getLogger(__name__)

INFERENCE_LOG_EVENT = "llm_inference"
"""Message of the one structured log line written per inference request."""

_default_counter: TokenCounter | None = None
_warmup_thread: threading.Thread | None = None
_warmup_lock = threading.Lock()


def warm_token_counter() -> None:
    """Load the shared BPE counter. May download the encoding: never call it on the loop."""
    global _default_counter
    if _default_counter is None:
        _default_counter = TokenCounter()


def start_token_counter_warmup() -> threading.Thread:
    """Load the shared BPE counter once, on a daemon thread; returns that thread.

    ``tiktoken.get_encoding`` downloads the encoding when it is not cached, with no timeout.
    Done on the event loop, the first model request would freeze the worker (no broker
    heartbeats, no health checks). Until the thread finishes, requests use the heuristic.
    """
    global _warmup_thread
    with _warmup_lock:
        if _warmup_thread is None:
            _warmup_thread = threading.Thread(
                target=warm_token_counter, name="token-counter-warmup", daemon=True
            )
            _warmup_thread.start()
        return _warmup_thread


def _heuristic_tokens(text: str) -> int:
    """Same estimate as TokenCounter's fallback: about 1.33 tokens per word."""
    words = text.split()
    return max(1, int(len(words) * 1.33)) if words else 0


def count_context_tokens(
    messages: Sequence[ChatMessage], token_counter: TokenCounter | None = None
) -> int:
    """Token size of the messages exactly as they are sent to the model."""
    counter = token_counter or _default_counter
    if counter is None:
        # Never build the BPE counter here: see start_token_counter_warmup.
        return sum(_heuristic_tokens(f"{m.role}\n{m.content}") for m in messages)
    return sum(counter.count_tokens(f"{m.role}\n{m.content}") for m in messages)


def _provenance(result: LLMResult | None, error: BaseException | None) -> dict[str, Any] | None:
    """The served-provider record of the call, from its result or its error; None when absent."""
    call = result.provenance if result is not None else getattr(error, "provenance", None)
    return call.to_dict() if isinstance(call, CallProvenance) else None


def record_inference(
    metrics: Any | None,
    *,
    kind: str,
    tier: str,
    context_tokens: int,
    latency_ms: int,
    result: LLMResult | None,
    error: BaseException | None,
    price_table: Mapping[str, ModelPricing] | None,
) -> None:
    """Record one inference request. Never raises.

    Always observes ``llm_context_tokens``. Tokens and cost are recorded only when the
    provider returned a result; a failed request has no billed usage to report. Cost is
    recorded only when ``price_table`` prices the model.

    The log line carries the call's ``provenance`` when the provider asked a router for it
    (which provider served the call, the attempt, the generation id; ``packages/llm/
    provenance.py``), from the result or from the error that carried it (a provider mismatch,
    an unparseable answer), so every call of a pinned route is recorded where it ran: triage,
    the summarizer, generation and repair alike (R21.4). None otherwise.
    """
    cost: float | None = None
    if result is not None and price_table is not None:
        try:
            cost = estimate_inference_cost(
                result.model, result.input_tokens, result.output_tokens, price_table
            )
        except ValueError:
            cost = None
    if metrics is not None:
        try:
            metrics.llm_context_tokens.labels(kind=kind, tier=tier).observe(context_tokens)
            if result is not None:
                if price_table is not None:
                    record_ai_cost(
                        result.model,
                        tier,
                        result.input_tokens,
                        result.output_tokens,
                        price_table,
                        metrics=metrics,
                    )
                else:
                    metrics.input_tokens_total.labels(model=result.model, tier=tier).inc(
                        result.input_tokens
                    )
                    metrics.output_tokens_total.labels(model=result.model, tier=tier).inc(
                        result.output_tokens
                    )
        except Exception:
            logger.warning("Inference metrics emission failed", exc_info=True)
    with contextlib.suppress(Exception):  # logging must never fail the call
        logger.info(
            INFERENCE_LOG_EVENT,
            extra={
                "fields": {
                    "kind": kind,
                    "tier": tier,
                    "model": result.model if result is not None else None,
                    "context_tokens": context_tokens,
                    "input_tokens": result.input_tokens if result is not None else None,
                    "output_tokens": result.output_tokens if result is not None else None,
                    "latency_ms": latency_ms,
                    "outcome": "ok" if error is None else type(error).__name__,
                    "estimated_cost_usd": cost,
                    "provenance": _provenance(result, error),
                }
            },
        )


async def instrumented_call(
    provider: LLMProvider,
    *,
    kind: str,
    messages: list[ChatMessage],
    metrics: Any | None,
    price_table: Mapping[str, ModelPricing] | None,
    schema: dict[str, Any] | None = None,
    tier: ModelTier = ModelTier.FAST,
    max_tokens: int = 1000,
    temperature: float = 0.0,
    **params: Any,
) -> LLMResult:
    """Call ``provider.generate`` and record the request exactly once, regardless of outcome.

    The single call site for the instrumentation sequence every request needs: context size is
    counted from the messages before the call, ``record_inference`` runs in ``finally`` whether
    the provider returns or raises, and the provider's own exception always reaches the caller
    unchanged. Shared by ``BudgetedLLMProvider`` (which knows each call's kind) and
    ``InstrumentedLLMProvider`` (fixed-kind callers), so a future change to the recording logic
    has one call site instead of two that can drift.
    """
    context_tokens = count_context_tokens(messages)
    started = time.perf_counter()
    result: LLMResult | None = None
    error: BaseException | None = None
    try:
        result = await provider.generate(
            messages=messages,
            schema=schema,
            tier=tier,
            max_tokens=max_tokens,
            temperature=temperature,
            **params,
        )
        return result
    except BaseException as exc:
        error = exc
        raise
    finally:
        record_inference(
            metrics,
            kind=kind,
            tier=str(result.tier) if result is not None else str(tier),
            context_tokens=context_tokens,
            latency_ms=(
                result.latency_ms
                if result is not None
                else int((time.perf_counter() - started) * 1000)
            ),
            result=result,
            error=error,
            price_table=price_table,
        )
