from __future__ import annotations

import json

from mailguard.contracts.policy import GuardReport, PolicyAction, RiskTier
from mailguard.contracts.verdict import (
    ChunkVerdict,
    Finding,
    LayerName,
    LayerVerdict,
    OutputVerdict,
    SanitizedIntent,
    Severity,
    StrippedSegment,
    ThreatType,
)
from mailguard.layers.l5_policy_engine import PolicyEngine


def finding(layer, tt, score):
    return Finding(
        layer=layer,
        threat_type=tt,
        severity=Severity.from_score(score),
        score=score,
        detector="rule",
    )


def test_policy_loads(settings):
    engine = PolicyEngine(settings)
    assert engine.version.startswith("2026")
    assert [r.priority for r in engine.rules] == sorted(r.priority for r in engine.rules)
    assert engine.rules[-1].id == "P99-default"


def test_clean_report_defaults_to_draft_only(settings):
    engine = PolicyEngine(settings)
    report = GuardReport(message_id="m", l1=LayerVerdict(layer=LayerName.L1_INJECTION_SCANNER))
    d = engine.decide(report, stage="inbound", category="support")
    assert d.action is PolicyAction.DRAFT_ONLY
    assert d.risk_tier is RiskTier.T0_CLEAN
    assert d.matched_rule_id == "P99-default"
    assert not d.requires_human


def test_critical_quarantines_and_is_idempotent(settings):
    engine = PolicyEngine(settings)
    l1 = LayerVerdict(
        layer=LayerName.L1_INJECTION_SCANNER,
        severity=Severity.CRITICAL,
        score=0.97,
        findings=[finding(LayerName.L1_INJECTION_SCANNER, ThreatType.INSTRUCTION_OVERRIDE, 0.97)],
    )
    report = GuardReport(message_id="m", organization_id="o", l1=l1)
    d1 = engine.decide(report, stage="inbound")
    d2 = engine.decide(report, stage="inbound")
    assert d1.action is PolicyAction.QUARANTINE and d1.requires_human
    assert d1.audit_id == d2.audit_id
    assert d1.risk_tier is RiskTier.T4_CRITICAL


def test_exfiltration_high_blocks(settings):
    engine = PolicyEngine(settings)
    l1 = LayerVerdict(
        layer=LayerName.L1_INJECTION_SCANNER,
        severity=Severity.HIGH,
        score=0.93,
        findings=[finding(LayerName.L1_INJECTION_SCANNER, ThreatType.DATA_EXFILTRATION, 0.93)],
    )
    d = engine.decide(GuardReport(l1=l1), stage="inbound")
    assert d.action is PolicyAction.BLOCK
    assert d.matched_rule_id == "P02-exfiltration-or-tool-abuse-block"


def test_outbound_compliance_blocks_and_ratio_escalates(settings):
    engine = PolicyEngine(settings)
    l4 = OutputVerdict(severity=Severity.HIGH, score=0.95, complied_with_injected_goal=True)
    d = engine.decide(GuardReport(l4=l4), stage="outbound")
    assert (
        d.action is PolicyAction.BLOCK and d.matched_rule_id == "P03-injected-goal-compliance-block"
    )

    chunks = [
        ChunkVerdict(chunk_id=f"c{i}", quarantined=i < 2, severity=Severity.MEDIUM, score=0.6)
        for i in range(3)
    ]
    d2 = engine.decide(GuardReport(l3b=chunks), stage="outbound")
    assert d2.action is PolicyAction.HUMAN_APPROVAL
    assert d2.matched_rule_id == "P04-rag-poisoning-escalate"
    assert d2.quarantined_chunk_ids == ["c0", "c1"]
    assert d2.risk_tier is RiskTier.T3_HIGH


def test_layer_error_fail_closed(settings):
    engine = PolicyEngine(settings)
    l1 = LayerVerdict(
        layer=LayerName.L1_INJECTION_SCANNER, severity=Severity.HIGH, score=0.9, error="boom"
    )
    d = engine.decide(GuardReport(l1=l1), stage="inbound")
    assert d.matched_rule_id == "P00-internal-error-fail-closed"
    assert d.action is PolicyAction.HUMAN_APPROVAL


def test_auto_send_only_for_allowed_categories(settings):
    engine = PolicyEngine(settings)
    clean = GuardReport(l4=OutputVerdict(severity=Severity.NONE))
    d = engine.decide(clean, stage="outbound", category="acknowledgement", draft_action="reply")
    assert d.action is PolicyAction.AUTO_SEND
    d2 = engine.decide(clean, stage="outbound", category="support", draft_action="reply")
    assert d2.action is PolicyAction.DRAFT_ONLY
    d3 = engine.decide(clean, stage="outbound", category="acknowledgement", draft_action="forward")
    assert d3.action is PolicyAction.DRAFT_ONLY


def test_heavy_sanitization_and_audit_log(settings, tmp_path):
    engine = PolicyEngine(settings)
    l2 = SanitizedIntent(
        sanitized_body="short",
        stripped_segments=[
            StrippedSegment(text="x" * 40, reason="rule:r", score=0.5, span_start=0, span_end=40)
        ],
    )
    d = engine.decide(GuardReport(message_id="m1", l2=l2), stage="inbound")
    assert d.matched_rule_id == "P09-heavy-sanitization-draft-only"
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["message_id"] == "m1" and rec["action"] == "draft_only"
    assert "short" not in json.dumps(rec)  # no content in the audit log
