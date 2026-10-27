"""Build a ``RunResult`` from one real guard run (design.md "core/runner.py", step 3).

Requirements 4, 5 and 15.5. Input is what ``MailGuardPipeline.run`` returned: the
``GuardReport``, the guarded draft (``None`` when the inbound gate stopped the job) and the
prompt bundle (``None`` likewise). The guard objects are read by attribute only; ``mailguard``
is imported lazily (for ``GuardConfig.preset``) so this module imports without the guard.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from services.guard_playground.core.redact import redact_data
from services.guard_playground.core.schema import (
    LAYER_ORDER,
    ChunkRow,
    Decision,
    EmailInput,
    Finding,
    LayerRow,
    RunResult,
    Stripped,
    Thresholds,
)

NotRunReason = Literal["disabled by preset", "stopped by policy"]
DISABLED: NotRunReason = "disabled by preset"
STOPPED: NotRunReason = "stopped by policy"
BLOCKING_ACTIONS = ("block", "quarantine")


def _value(enum_or_str: Any) -> str:
    """``StrEnum`` member or plain string -> its string value."""
    return str(getattr(enum_or_str, "value", enum_or_str))


def _findings(verdict: Any) -> list[Finding]:
    return [
        Finding(
            detector=str(f.detector),
            rule_id=f.rule_id,
            score=float(f.score),
            technique=f.technique,
            excerpt=f.excerpt or "",
        )
        for f in verdict.findings
    ]


def _fallback_reason(verdict: Any) -> str | None:
    """``llm_fallback_reason`` from the verdict, its inbound stages, or its chunks."""
    metadata = verdict.metadata or {}
    if metadata.get("llm_fallback_reason"):
        return str(metadata["llm_fallback_reason"])
    stages = metadata.get("stages") or {}
    for name in ("scan", "extract"):
        record = stages.get(name)
        if isinstance(record, dict):
            reason = (record.get("metadata") or {}).get("llm_fallback_reason")
            if reason:
                return str(reason)
    for chunk in getattr(verdict, "chunks", None) or []:
        reason = (chunk.metadata or {}).get("llm_fallback_reason")
        if reason:
            return str(reason)
    return None


def _ran_row(layer: str, verdict: Any, judge_configured: bool) -> LayerRow:
    return LayerRow(
        layer=layer,
        ran=True,
        not_run_reason=None,
        severity=_value(verdict.severity),
        score=float(verdict.score),
        decided_by=str(verdict.decided_by),
        latency_ms=int(verdict.latency_ms),
        findings=_findings(verdict),
        error=verdict.error,
        fallback_reason=_fallback_reason(verdict),
        judge_configured=judge_configured,
    )


def _empty_row(
    layer: str,
    *,
    ran: bool,
    reason: NotRunReason | None,
    judge_configured: bool,
) -> LayerRow:
    return LayerRow(
        layer=layer,
        ran=ran,
        not_run_reason=reason,
        severity="none",
        score=0.0,
        decided_by="none",
        latency_ms=0,
        findings=[],
        error=None,
        fallback_reason=None,
        judge_configured=judge_configured,
    )


def _decision(decision: Any) -> Decision | None:
    if decision is None:
        return None
    return Decision(
        action=_value(decision.action),
        matched_rule_id=str(decision.matched_rule_id),
        risk_tier=_value(decision.risk_tier),
        requires_human=bool(decision.requires_human),
    )


def _stopped_at_pass_one(report: Any) -> bool:
    decision = report.inbound_decision
    return decision is not None and _value(decision.action) in BLOCKING_ACTIONS


def _gate_passes(report: Any) -> int | None:
    """``report.metadata["gate_passes"]``, else the inbound decision's own record of it."""
    value = (report.metadata or {}).get("gate_passes")
    if value is None and report.inbound_decision is not None:
        value = (report.inbound_decision.metadata or {}).get("gate_passes")
    return int(value) if value is not None else None


def _layer_rows(
    report: Any, config: Any, email: EmailInput, judges: Mapping[str, bool]
) -> list[LayerRow]:
    verdicts = {
        "inbound_guard": report.inbound_guard,
        "doc_scanner": report.doc_scanner,
        "output_guard": report.output_guard,
    }
    rows: list[LayerRow] = []
    for layer in LAYER_ORDER:
        judge = bool(judges.get(layer, True))
        verdict = verdicts[layer]
        if verdict is not None:
            rows.append(_ran_row(layer, verdict, judge))
        elif not getattr(config, layer):
            rows.append(_empty_row(layer, ran=False, reason=DISABLED, judge_configured=judge))
        elif layer == "doc_scanner" and not email.chunks and not _stopped_at_pass_one(report):
            # enabled with nothing to scan: it ran over 0 chunks ("0 chunks scanned")
            rows.append(_empty_row(layer, ran=True, reason=None, judge_configured=judge))
        else:
            rows.append(_empty_row(layer, ran=False, reason=STOPPED, judge_configured=judge))
    return rows


def _chunk_rows(report: Any, email: EmailInput) -> list[ChunkRow]:
    docs = report.doc_scanner
    by_id = {c.chunk_id: c for c in docs.chunks} if docs is not None else {}
    rows: list[ChunkRow] = []
    for chunk in email.chunks:
        verdict = by_id.get(chunk.chunk_id)
        score: float | None = None
        quarantined = False
        rule_id = technique = None
        if verdict is not None:
            score = float(verdict.score)
            quarantined = bool(verdict.quarantined)
            if quarantined and verdict.findings:
                top = max(verdict.findings, key=lambda f: f.score)
                rule_id, technique = top.rule_id, top.technique
        rows.append(
            ChunkRow(
                chunk_id=chunk.chunk_id,
                content=chunk.content,
                score=score,
                quarantined=quarantined,
                reason_rule_id=rule_id,
                reason_technique=technique,
            )
        )
    return rows


def run_result_from_guard(
    report: Any,
    draft: Any,
    bundle: Any,
    *,
    email: EmailInput,
    preset: str,
    mode: Literal["live", "replay"],
    draft_model: str,
    guard_models: dict[str, str],
    settings: Any,
    generated_at: datetime | None = None,
    judges_configured: Mapping[str, bool] | None = None,
) -> RunResult:
    """Map ``pipeline.run``'s ``(report, draft, bundle)`` onto the closed ``RunResult`` schema.

    ``settings`` is the ``MailGuardSettings`` the pipeline actually used (thresholds come from
    it). ``judges_configured`` says, per layer, whether its LLM judge provider existed
    (default: all True). The result is passed through ``redact_data`` before it is returned.
    """
    from mailguard.pipeline import GuardConfig

    config = GuardConfig.preset(preset)
    judges = dict(judges_configured or {})
    inbound = report.inbound_guard
    docs = report.doc_scanner

    outbound = None
    if report.output_guard is not None and report.decision is not report.inbound_decision:
        outbound = _decision(report.decision)

    result = RunResult(
        generated_at=generated_at or datetime.now(UTC),
        mode=mode,
        source=email.source,
        case_id=email.case_id,
        preset=preset,
        draft_model=draft_model,
        guard_models=dict(guard_models),
        email=email,
        layers=_layer_rows(report, config, email, judges),
        user_intent=(inbound.user_intent or None) if inbound is not None else None,
        stripped=[
            Stripped(text=s.text, reason=s.reason, score=float(s.score))
            for s in (inbound.stripped_segments if inbound is not None else [])
        ],
        removed_ratio=float(inbound.removed_ratio) if inbound is not None else 0.0,
        chunks=_chunk_rows(report, email),
        quarantine_ratio=float(docs.quarantine_ratio) if docs is not None else 0.0,
        inbound=_decision(report.inbound_decision),
        outbound=outbound,
        gate_passes=_gate_passes(report),
        isolation_applied=report.isolation is not None and bool(report.isolation.enabled),
        draft_generated=draft is not None,
        draft_body=draft.body if draft is not None else None,
        guarded_prompt=bundle.messages[1].content if bundle is not None else None,
        thresholds=Thresholds(
            block=settings.inbound_guard.block_threshold,
            flag=settings.inbound_guard.flag_threshold,
            strip=settings.inbound_guard.strip_threshold,
            quarantine=settings.doc_scanner.quarantine_threshold,
        ),
    )
    redacted: RunResult = redact_data(result)
    return redacted


__all__ = ["run_result_from_guard"]
