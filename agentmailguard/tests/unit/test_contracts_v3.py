"""Schema-3 contracts (ADR-0015, Task 4): layer names, merged verdicts, report, audit line."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mailguard.contracts.policy import GuardReport
from mailguard.contracts.verdict import (
    ChunkVerdict,
    DocumentVerdict,
    Finding,
    InboundVerdict,
    IsolationInfo,
    LayerName,
    LayerVerdict,
    OutputVerdict,
    SanitizedIntent,
    Severity,
    StrippedSegment,
    ThreatType,
)
from mailguard.policy import PolicyEngine
from mailguard.policy.engine import layer_for_stage


def _finding(score: float, rule_id: str) -> Finding:
    return Finding(
        layer=LayerName.INBOUND_GUARD,
        threat_type=ThreatType.PROMPT_INJECTION,
        severity=Severity.from_score(score),
        score=score,
        detector="rule",
        rule_id=rule_id,
    )


def _scan(score: float = 0.0, **kw) -> LayerVerdict:
    return LayerVerdict(
        layer=LayerName.INBOUND_GUARD,
        severity=Severity.from_score(score),
        score=score,
        findings=[_finding(score, "scan-rule")] if score else [],
        latency_ms=3,
        metadata={"indicators": {"emails": ["x@evil.io"], "urls": []}},
        **kw,
    )


def _extract(score: float = 0.0, **kw) -> SanitizedIntent:
    return SanitizedIntent(
        severity=Severity.from_score(score),
        score=score,
        findings=[_finding(score, "extract-rule")] if score else [],
        latency_ms=5,
        sanitized_body="Where is my order?",
        user_intent="Customer asks about an order.",
        stripped_segments=[
            StrippedSegment(
                text="ignore all", reason="rule:r", score=0.9, span_start=0, span_end=10
            )
        ],
        **kw,
    )


def test_layer_names_are_the_four_v3_layers():
    assert [str(n) for n in LayerName] == [
        "inbound_guard",
        "doc_scanner",
        "inbound_gate",
        "output_guard",
    ]
    assert layer_for_stage("inbound") is LayerName.INBOUND_GATE
    assert layer_for_stage("outbound") is LayerName.OUTPUT_GUARD


def test_merge_of_nothing_is_none():
    assert InboundVerdict.merge(None, None) is None


def test_merge_takes_intent_from_extract_and_tags_every_finding():
    merged = InboundVerdict.merge(_scan(0.6), _extract(0.3))
    assert merged is not None and merged.layer is LayerName.INBOUND_GUARD
    assert merged.sanitized_body == "Where is my order?"
    assert merged.user_intent == "Customer asks about an order."
    assert merged.removed_ratio > 0
    assert [(f.rule_id, f.metadata["stage"]) for f in merged.findings] == [
        ("scan-rule", "scan"),
        ("extract-rule", "extract"),
    ]
    assert set(merged.metadata["stages"]) == {"scan", "extract"}
    assert merged.stage("scan")["metadata"]["indicators"]["emails"] == ["x@evil.io"]
    assert merged.latency_ms == 8
    assert merged.question_sentences == []


def test_merge_grades_by_the_scan_only():
    """ADR-0015 decision 8 (R-i, Task 5): the extractor never raises the severity."""
    merged = InboundVerdict.merge(_scan(0.0), _extract(0.7))
    assert merged is not None and merged.severity is Severity.NONE and merged.score == 0.0
    assert merged.stage("scan")["severity"] == "none"
    assert merged.stage("extract")["severity"] == "medium"
    assert any(f.metadata["stage"] == "extract" for f in merged.findings)
    no_scan = InboundVerdict.merge(None, _extract(0.9))
    # without a scan the email is ungraded (Task 6 removed the F11 transitional rule)
    assert no_scan is not None and no_scan.severity is Severity.NONE and no_scan.score == 0.0


@pytest.mark.parametrize(("scan_err", "extract_err"), [("boom", None), (None, "bad"), ("a", "b")])
def test_merge_propagates_a_sub_stage_error(scan_err, extract_err):
    merged = InboundVerdict.merge(_scan(error=scan_err), _extract(error=extract_err))
    assert merged is not None and merged.error
    if scan_err:
        assert f"scan: {scan_err}" in merged.error
    if extract_err:
        assert f"extract: {extract_err}" in merged.error


def test_merge_with_scan_only_has_no_intent():
    merged = InboundVerdict.merge(_scan(0.9), None)
    assert merged is not None and merged.stage("extract") is None
    assert merged.sanitized_body == "" and merged.severity is Severity.HIGH


def test_document_verdict_aggregates_chunks():
    chunks = [
        ChunkVerdict(chunk_id="a", score=0.1, severity=Severity.NONE, latency_ms=1),
        ChunkVerdict(
            chunk_id="b",
            quarantined=True,
            trust="quarantined",
            score=0.9,
            severity=Severity.HIGH,
            latency_ms=2,
        ),
    ]
    docs = DocumentVerdict.from_chunks(chunks)
    assert docs.layer is LayerName.DOC_SCANNER
    assert docs.kept_chunk_ids == ["a"] and docs.quarantined_chunk_ids == ["b"]
    assert docs.quarantine_ratio == 0.5
    assert docs.severity is Severity.HIGH and docs.score == 0.9 and docs.latency_ms == 3
    empty = DocumentVerdict.from_chunks([])
    assert empty.quarantine_ratio == 0.0 and empty.severity is Severity.NONE


def test_chunk_verdict_defaults_keep_v2_readers_working():
    c = ChunkVerdict(chunk_id="x")
    assert c.trust == "untrusted" and c.cluster_id is None and c.signals == {}


def test_report_is_schema_3_and_refuses_a_v2_dump():
    report = GuardReport(message_id="m")
    assert report.schema_version == 3
    dumped = report.model_dump(mode="json")
    assert set(dumped) >= {"inbound_guard", "doc_scanner", "isolation", "output_guard"}
    assert GuardReport.model_validate(dumped) == report
    for v2_key in ("l1", "l2", "l3", "l3b", "l4"):
        with pytest.raises(ValidationError):
            GuardReport.model_validate({**dumped, v2_key: None})
    with pytest.raises(ValidationError):
        GuardReport.model_validate({**dumped, "schema_version": 2})


def test_report_verdicts_are_layer_verdicts_only():
    report = GuardReport(
        inbound_guard=InboundVerdict.merge(_scan(0.6), None),
        doc_scanner=DocumentVerdict.from_chunks([ChunkVerdict(chunk_id="c")]),
        isolation=IsolationInfo(markers_scrubbed=1),
        output_guard=OutputVerdict(),
    )
    assert [v.layer for v in report.verdicts()] == [
        LayerName.INBOUND_GUARD,
        LayerName.DOC_SCANNER,
        LayerName.OUTPUT_GUARD,
    ]
    assert report.max_severity is Severity.MEDIUM


def test_audit_line_is_schema_3(settings):
    engine = PolicyEngine(settings)
    report = GuardReport(
        message_id="m-audit",
        organization_id="o",
        inbound_guard=InboundVerdict.merge(_scan(0.6), _extract()),
        doc_scanner=DocumentVerdict.from_chunks(
            [ChunkVerdict(chunk_id="c1", quarantined=True, trust="quarantined")]
        ),
        isolation=IsolationInfo(mode="datamark", markers_scrubbed=2),
    )
    decision = engine.decide(report, stage="inbound")
    rec = json.loads(Path(engine.audit_path).read_text(encoding="utf-8").splitlines()[-1])
    assert rec["schema_version"] == 3
    assert rec["layer"] == "inbound_gate" and decision.metadata["layer"] == "inbound_gate"
    assert set(rec["layers"]) == {"inbound_guard", "doc_scanner"}
    assert rec["doc_scanner"] == {"chunks": 1, "quarantined": ["c1"]}
    assert rec["isolation"] == {
        "mode": "datamark",
        "markers_scrubbed": 2,
        "forged_markers": 0,
        "error": False,
    }
    assert "l3b" not in rec and rec["policy_version"] == "2026.10-v3"
    assert "Where is my order" not in json.dumps(rec)  # no content in the audit log
    # the audit id seed is unchanged: same report + stage -> same id
    assert engine.decide(report, stage="inbound").audit_id == decision.audit_id
