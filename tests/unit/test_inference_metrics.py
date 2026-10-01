"""Per-inference telemetry: context size, tokens, cost and one log line (R11.7, R21.4, R21.6)."""

from __future__ import annotations

import json
import logging

import pytest

from packages.core.settings import ModelPricing
from packages.llm.inference_metrics import (
    INFERENCE_LOG_EVENT,
    count_context_tokens,
    record_inference,
)
from packages.llm.protocol import (
    CallProvenance,
    ChatMessage,
    LLMProviderMismatchError,
    LLMResult,
    ModelTier,
)
from packages.observability.logging import StructuredJSONFormatter
from packages.observability.metrics import PipelineMetrics, create_pipeline_metrics

PRICES = {"model-a": ModelPricing(input_per_m=0.15, output_per_m=0.60)}
MESSAGES = [
    ChatMessage(role="system", content="You are a support assistant."),
    ChatMessage(role="user", content="How do I reset my password?"),
]


def _result(model: str = "model-a") -> LLMResult:
    return LLMResult(
        content={}, model=model, tier=ModelTier.ROUTINE, input_tokens=1_200, output_tokens=300
    )


def _sample(m: PipelineMetrics, name: str, labels: dict[str, str]) -> float | None:
    return m.registry.get_sample_value(name, labels)


def _inference_fields(caplog: pytest.LogCaptureFixture) -> dict[str, object]:
    records = [r for r in caplog.records if r.getMessage() == INFERENCE_LOG_EVENT]
    assert len(records) == 1
    fields = records[0].__dict__["fields"]
    assert isinstance(fields, dict)
    return fields


def test_context_tokens_cover_every_message() -> None:
    one = count_context_tokens(MESSAGES[:1])
    both = count_context_tokens(MESSAGES)
    assert 0 < one < both
    assert count_context_tokens([]) == 0


def test_successful_request_records_context_tokens_and_cost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    m = create_pipeline_metrics()
    caplog.set_level(logging.INFO)

    record_inference(
        m,
        kind="generate",
        tier="routine",
        context_tokens=812,
        latency_ms=900,
        result=_result(),
        error=None,
        price_table=PRICES,
    )

    labels = {"kind": "generate", "tier": "routine"}
    assert _sample(m, "llm_context_tokens_count", labels) == 1
    assert _sample(m, "llm_context_tokens_sum", labels) == 812
    tok = {"model": "model-a", "tier": "routine"}
    assert _sample(m, "input_tokens_total", tok) == 1_200
    assert _sample(m, "output_tokens_total", tok) == 300
    assert _sample(m, "estimated_ai_cost_total", tok) == pytest.approx(0.00036)
    fields = _inference_fields(caplog)
    assert fields["kind"] == "generate"
    assert fields["context_tokens"] == 812
    assert fields["outcome"] == "ok"
    assert fields["estimated_cost_usd"] == pytest.approx(0.00036)
    assert "content" not in fields


def test_unpriced_model_counts_tokens_but_not_cost(caplog: pytest.LogCaptureFixture) -> None:
    """Review Focus 3."""
    m = create_pipeline_metrics()
    caplog.set_level(logging.INFO)

    record_inference(
        m,
        kind="generate",
        tier="routine",
        context_tokens=10,
        latency_ms=5,
        result=_result("model-unpriced"),
        error=None,
        price_table=PRICES,
    )

    tok = {"model": "model-unpriced", "tier": "routine"}
    assert _sample(m, "input_tokens_total", tok) == 1_200
    assert _sample(m, "estimated_ai_cost_total", tok) is None
    assert _inference_fields(caplog)["estimated_cost_usd"] is None


def test_no_price_table_counts_tokens_only() -> None:
    m = create_pipeline_metrics()
    record_inference(
        m,
        kind="triage",
        tier="fast",
        context_tokens=10,
        latency_ms=5,
        result=_result(),
        error=None,
        price_table=None,
    )
    tok = {"model": "model-a", "tier": "fast"}
    assert _sample(m, "input_tokens_total", tok) == 1_200
    assert _sample(m, "estimated_ai_cost_total", tok) is None


def test_failed_request_records_context_and_outcome_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    m = create_pipeline_metrics()
    caplog.set_level(logging.INFO)

    record_inference(
        m,
        kind="generate",
        tier="routine",
        context_tokens=640,
        latency_ms=30_000,
        result=None,
        error=TimeoutError("upstream"),
        price_table=PRICES,
    )

    assert _sample(m, "llm_context_tokens_count", {"kind": "generate", "tier": "routine"}) == 1
    assert _sample(m, "input_tokens_total", {"model": "model-a", "tier": "routine"}) is None
    fields = _inference_fields(caplog)
    assert fields["outcome"] == "TimeoutError"
    assert fields["input_tokens"] is None


SERVED = CallProvenance(
    requested_model="model-a", served_provider="CoreWeave", attempt=1, generation_id="gen-1"
)


def _log_one(caplog: pytest.LogCaptureFixture, **call: object) -> dict[str, object]:
    caplog.set_level(logging.INFO)
    record_inference(
        None,
        kind="triage",
        tier="routine",
        context_tokens=10,
        latency_ms=5,
        price_table=None,
        **call,  # type: ignore[arg-type]
    )
    return _inference_fields(caplog)


def test_the_log_line_records_which_provider_served_the_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A pinned route records the served provider of every call where it ran (task 7.29):
    # triage and the summarizer too, whose results reach no benchmark row.
    result = _result()
    result.provenance = SERVED
    fields = _log_one(caplog, result=result, error=None)
    assert fields["provenance"] == SERVED.to_dict()


def test_a_mismatch_error_logs_the_provider_that_served_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    other = CallProvenance(requested_model="model-a", served_provider="DeepInfra", attempt=2)
    error = LLMProviderMismatchError(
        "provider_mismatch: x", expected=("coreweave",), served="DeepInfra", provenance=other
    )
    fields = _log_one(caplog, result=None, error=error)
    assert fields["outcome"] == "LLMProviderMismatchError"
    assert fields["provenance"] == other.to_dict()


def test_a_call_without_router_metadata_logs_no_provenance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert _log_one(caplog, result=_result(), error=None)["provenance"] is None


def test_broken_metrics_never_raise() -> None:
    """Review Focus 4: telemetry must never fail the call it measures."""
    record_inference(
        object(),
        kind="generate",
        tier="routine",
        context_tokens=1,
        latency_ms=1,
        result=_result(),
        error=None,
        price_table=PRICES,
    )
    record_inference(
        None,
        kind="generate",
        tier="routine",
        context_tokens=1,
        latency_ms=1,
        result=_result(),
        error=None,
        price_table=PRICES,
    )


def test_new_and_changed_collectors_use_low_cardinality_labels() -> None:
    m = create_pipeline_metrics()
    allowed = {
        "organization",
        "category",
        "priority",
        "model_tier",
        "decided_by",
        "kind",
        "tier",
        "model",
    }
    for collector in (m.llm_context_tokens, m.generated_draft_cost_total, m.emails_generated_total):
        assert set(collector._labelnames) <= allowed
    assert set(m.emails_generated_total._labelnames) == {"organization", "category", "model_tier"}


def test_json_formatter_emits_structured_fields() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, INFERENCE_LOG_EVENT, None, None)
    record.fields = {"kind": "generate", "context_tokens": 812}
    payload = json.loads(StructuredJSONFormatter().format(record))
    assert payload["fields"] == {"kind": "generate", "context_tokens": 812}


class _RecordingCounter:
    """Stands in for the BPE counter; records the thread that built it."""

    built_on: list[str] = []

    def __init__(self) -> None:
        import threading

        _RecordingCounter.built_on.append(threading.current_thread().name)

    def count_tokens(self, text: str) -> int:
        return 7


def test_request_path_never_loads_the_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loading the BPE encoding may download it with no timeout; never on the caller's loop."""
    import packages.llm.inference_metrics as im

    def _fail() -> None:
        raise AssertionError("encoding loaded on the request path")

    monkeypatch.setattr(im, "_default_counter", None)
    monkeypatch.setattr(im, "TokenCounter", _fail)

    assert count_context_tokens(MESSAGES) > 0


def test_warmed_counter_is_used_for_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    import packages.llm.inference_metrics as im

    monkeypatch.setattr(im, "_default_counter", None)
    monkeypatch.setattr(im, "TokenCounter", _RecordingCounter)

    im.warm_token_counter()

    assert count_context_tokens(MESSAGES) == 7 * len(MESSAGES)


def test_warmup_runs_once_off_the_calling_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    import packages.llm.inference_metrics as im

    monkeypatch.setattr(im, "_default_counter", None)
    monkeypatch.setattr(im, "_warmup_thread", None)
    monkeypatch.setattr(im, "TokenCounter", _RecordingCounter)
    _RecordingCounter.built_on = []

    first = im.start_token_counter_warmup()
    second = im.start_token_counter_warmup()
    assert first is not None
    first.join(timeout=5)

    assert second is first
    assert _RecordingCounter.built_on == [first.name]
    assert first.name != threading.current_thread().name


def test_generation_latency_has_a_bucket_at_the_nfr8_limit() -> None:
    """NFR8 (LLM generation 1-5 s): p95 > 5000 ms must be read at a real bucket edge."""
    from packages.observability.metrics import GENERATION_BUCKETS

    assert 1000.0 in GENERATION_BUCKETS
    assert 5000.0 in GENERATION_BUCKETS
    m = create_pipeline_metrics()
    m.generation_latency_ms.labels(model="model-a", tier="routine").observe(4500)
    assert (
        m.registry.get_sample_value(
            "generation_latency_ms_bucket", {"model": "model-a", "tier": "routine", "le": "5000.0"}
        )
        == 1
    )
