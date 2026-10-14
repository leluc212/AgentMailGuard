"""Job result contract to the core: v2 keys and values are unchanged, three keys are added."""

from __future__ import annotations

from mailguard.contracts.email import DraftCandidate
from mailguard.contracts.policy import GuardReport, PolicyAction, PolicyDecision, RiskTier
from mailguard.integration.adapters import decision_to_job_result

V2_MAILGUARD_KEYS = {
    "action": "human_approval",
    "risk_tier": str(RiskTier.T3_HIGH),
    "audit_id": "audit-1",
    "rule": "P-x",
    "requires_human": True,
    "quarantined_chunks": ["c1"],
    "redactions": 2,
    "max_severity": "none",
    "latency_ms": 42,
}


def _report(decision: PolicyDecision | None) -> GuardReport:
    return GuardReport(message_id="m1", total_latency_ms=42, decision=decision)


def _decision(**kw: object) -> PolicyDecision:
    base: dict[str, object] = {
        "audit_id": "audit-1",
        "action": PolicyAction.HUMAN_APPROVAL,
        "risk_tier": RiskTier.T3_HIGH,
        "matched_rule_id": "P-x",
        "reasons": ["first reason", "second reason"],
        "requires_human": True,
        "redactions_applied": 2,
        "quarantined_chunk_ids": ["c1"],
        "metadata": {"decided_by": "output_guard"},
    }
    base.update(kw)
    return PolicyDecision(**base)  # type: ignore[arg-type]


def _draft() -> DraftCandidate:
    return DraftCandidate(body="b", subject="s", citations=["k"], action="reply")


def test_v2_keys_unchanged_and_new_keys_added() -> None:
    report = _report(_decision())
    result = decision_to_job_result(report, _draft())
    mg = result["mailguard"]
    assert {k: mg[k] for k in V2_MAILGUARD_KEYS} == V2_MAILGUARD_KEYS
    assert set(mg) - set(V2_MAILGUARD_KEYS) == {"reason", "decided_by", "guard_schema_version"}
    assert mg["reason"] == "first reason"
    assert mg["decided_by"] == "output_guard"
    assert mg["guard_schema_version"] == report.schema_version == 3
    assert result["draft"] == {"subject": "s", "body": "b", "citations": ["k"], "action": "reply"}
    assert set(result) == {"mailguard", "draft"}


def test_reason_is_none_when_no_reasons() -> None:
    mg = decision_to_job_result(_report(_decision(reasons=[])), None)["mailguard"]
    assert mg["reason"] is None


def test_decided_by_is_none_when_missing() -> None:
    mg = decision_to_job_result(_report(_decision(metadata={})), None)["mailguard"]
    assert mg["decided_by"] is None


def test_no_decision_gives_none_fields_but_keeps_schema_version() -> None:
    mg = decision_to_job_result(_report(None), None)["mailguard"]
    assert mg["reason"] is None
    assert mg["decided_by"] is None
    assert mg["guard_schema_version"] == 3
    assert mg["action"] == str(PolicyAction.DRAFT_ONLY)
