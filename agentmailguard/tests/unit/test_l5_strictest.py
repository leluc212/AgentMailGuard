"""L5 picks the strictest matching rule, not the first (ADR-0012 decision 2(c)).

Before: the first rule by priority number won, so a weaker action could pre-empt a stricter
one (P00 human_approval over P01 quarantine; P04/P05 human_approval over P06 block).
Now: among all matching rules the strictest action wins
(quarantine > block > human_approval > draft_only > auto_send); ties go to the lowest
priority number. A catch-all rule (empty ``when``) is the fallback for "nothing else
matched", never a competitor, so ``P10 auto_send`` still beats ``P99 draft_only``.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from mailguard.contracts.policy import GuardReport, PolicyAction
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


def finding(layer: LayerName, tt: ThreatType, score: float) -> Finding:
    return Finding(
        layer=layer,
        threat_type=tt,
        severity=Severity.from_score(score),
        score=score,
        detector="rule",
    )


def l1_verdict(severity: Severity, score: float, *tts: ThreatType) -> LayerVerdict:
    return LayerVerdict(
        layer=LayerName.L1_INJECTION_SCANNER,
        severity=severity,
        score=score,
        findings=[finding(LayerName.L1_INJECTION_SCANNER, tt, score) for tt in tts],
    )


def failed_l2() -> SanitizedIntent:
    return SanitizedIntent(severity=Severity.HIGH, score=0.9, decided_by="error", error="boom")


def unsafe_l4() -> OutputVerdict:
    return OutputVerdict(
        severity=Severity.HIGH,
        score=0.9,
        findings=[finding(LayerName.L4_OUTPUT_SCANNER, ThreatType.UNSAFE_ACTION, 0.9)],
    )


def quarantined_chunks(n_bad: int, n_total: int) -> list[ChunkVerdict]:
    return [
        ChunkVerdict(
            chunk_id=f"c{i}",
            quarantined=i < n_bad,
            severity=Severity.HIGH if i < n_bad else Severity.NONE,
            score=0.9 if i < n_bad else 0.0,
        )
        for i in range(n_total)
    ]


@pytest.fixture
def engine(settings) -> PolicyEngine:
    return PolicyEngine(settings)


# ------------------------------------------------------------------ the two defect cases
def test_layer_error_plus_critical_injection_quarantines(engine):
    report = GuardReport(
        message_id="m",
        l1=l1_verdict(Severity.CRITICAL, 0.98, ThreatType.INSTRUCTION_OVERRIDE),
        l2=failed_l2(),
    )
    d = engine.decide(report, stage="inbound")
    assert d.action is PolicyAction.QUARANTINE
    assert d.matched_rule_id == "P01-critical-injection-quarantine"
    assert d.requires_human
    assert "P00-internal-error-fail-closed" in d.matched_rule_ids


def test_layer_error_alone_is_still_human_approval(engine):
    d = engine.decide(GuardReport(l2=failed_l2()), stage="inbound")
    assert d.action is PolicyAction.HUMAN_APPROVAL
    assert d.matched_rule_id == "P00-internal-error-fail-closed"


def test_layer_error_plus_exfiltration_blocks(engine):
    report = GuardReport(
        l1=l1_verdict(Severity.HIGH, 0.93, ThreatType.DATA_EXFILTRATION), l2=failed_l2()
    )
    d = engine.decide(report, stage="inbound")
    assert d.action is PolicyAction.BLOCK
    assert d.matched_rule_id == "P02-exfiltration-or-tool-abuse-block"


def test_high_severity_unsafe_forward_blocks_instead_of_human_approval(engine):
    d = engine.decide(GuardReport(l4=unsafe_l4()), stage="outbound", draft_action="forward")
    assert d.action is PolicyAction.BLOCK
    assert d.matched_rule_id == "P06-outbound-unsafe-action-block"
    assert d.matched_rule_ids == [
        "P05-high-severity-human-approval",
        "P06-outbound-unsafe-action-block",
        "P08-medium-severity-draft-only",
    ]


def test_rag_poisoning_plus_unsafe_forward_blocks(engine):
    report = GuardReport(l3b=quarantined_chunks(2, 3), l4=unsafe_l4())
    d = engine.decide(report, stage="outbound", draft_action="forward")
    assert d.action is PolicyAction.BLOCK
    assert d.matched_rule_id == "P06-outbound-unsafe-action-block"
    assert d.matched_rule_ids == [
        "P04-rag-poisoning-escalate",
        "P05-high-severity-human-approval",
        "P06-outbound-unsafe-action-block",
        "P08-medium-severity-draft-only",
    ]
    assert d.quarantined_chunk_ids == ["c0", "c1"]


# ------------------------------------------------------------------ ties and recording
def test_equal_actions_go_to_the_lowest_priority_number(engine):
    # P04 and P05 are both human_approval; P04 (priority 30) must win, as before
    d = engine.decide(GuardReport(l3b=quarantined_chunks(2, 3)), stage="outbound")
    assert d.action is PolicyAction.HUMAN_APPROVAL
    assert d.matched_rule_id == "P04-rag-poisoning-escalate"
    assert d.matched_rule_ids == [
        "P04-rag-poisoning-escalate",
        "P05-high-severity-human-approval",
        "P08-medium-severity-draft-only",
    ]

    # P02 (20) and P03 (25) are both block
    report = GuardReport(
        l1=l1_verdict(Severity.HIGH, 0.93, ThreatType.DATA_EXFILTRATION),
        l4=OutputVerdict(severity=Severity.HIGH, score=0.95, complied_with_injected_goal=True),
    )
    d2 = engine.decide(report, stage="outbound")
    assert d2.matched_rule_id == "P02-exfiltration-or-tool-abuse-block"
    assert d2.matched_rule_ids[:2] == [
        "P02-exfiltration-or-tool-abuse-block",
        "P03-injected-goal-compliance-block",
    ]


def test_decision_takes_requires_human_and_reason_from_the_winner(engine):
    d = engine.decide(GuardReport(l4=unsafe_l4()), stage="outbound", draft_action="forward")
    assert d.reasons[0].startswith("Draft attempted a forward")
    assert d.requires_human


def test_audit_ids_stay_deterministic_and_track_the_winner(engine):
    report = GuardReport(message_id="m", organization_id="o", l4=unsafe_l4())
    d1 = engine.decide(report, stage="outbound", draft_action="forward")
    d2 = engine.decide(report, stage="outbound", draft_action="forward")
    assert d1.audit_id == d2.audit_id
    assert len(d1.audit_id) == 32
    other = engine.decide(
        GuardReport(message_id="m2", organization_id="o", l4=unsafe_l4()), stage="outbound"
    )
    assert other.audit_id != d1.audit_id


def test_audit_log_records_winner_and_all_matched_rules(settings, tmp_path):
    engine = PolicyEngine(settings)
    d = engine.decide(GuardReport(message_id="m1", l4=unsafe_l4()), stage="outbound")
    rec = json.loads(Path(engine.audit_path).read_text(encoding="utf-8").splitlines()[-1])
    assert rec["rule"] == d.matched_rule_id == "P06-outbound-unsafe-action-block"
    assert rec["matched_rules"] == d.matched_rule_ids
    assert rec["policy_version"] == engine.version


def test_policy_version_records_the_new_selection_semantics(engine):
    assert engine.version == "2026.09-v2"


# ------------------------------------------------------------------ fallback rule stays a fallback
def test_clean_report_uses_only_the_default_rule(engine):
    d = engine.decide(
        GuardReport(l1=LayerVerdict(layer=LayerName.L1_INJECTION_SCANNER)), stage="inbound"
    )
    assert d.action is PolicyAction.DRAFT_ONLY
    assert d.matched_rule_id == "P99-default"
    assert d.matched_rule_ids == ["P99-default"]


def test_catch_all_default_does_not_outrank_clean_auto_send(engine):
    clean = GuardReport(l4=OutputVerdict(severity=Severity.NONE))
    d = engine.decide(clean, stage="outbound", category="acknowledgement", draft_action="reply")
    assert d.action is PolicyAction.AUTO_SEND
    assert d.matched_rule_id == "P10-clean-auto-send"
    assert d.matched_rule_ids == ["P10-clean-auto-send"]


def test_heavy_sanitization_still_beats_auto_send(engine):
    l2 = SanitizedIntent(
        sanitized_body="ok",
        stripped_segments=[
            StrippedSegment(text="x" * 40, reason="rule:r", score=0.5, span_start=0, span_end=40)
        ],
    )
    d = engine.decide(
        GuardReport(l2=l2, l4=OutputVerdict(severity=Severity.NONE)),
        stage="outbound",
        category="acknowledgement",
        draft_action="reply",
    )
    assert d.action is PolicyAction.DRAFT_ONLY
    assert d.matched_rule_id == "P09-heavy-sanitization-draft-only"


# ------------------------------------------------------------------ regression against first-match
def legacy_decision(engine: PolicyEngine, facts: dict):
    """The old semantics: first matching rule by priority (default when none)."""
    return next((r for r in engine.rules if r.matches(facts)), None)


def report_space():
    l1s = [
        None,
        l1_verdict(Severity.NONE, 0.0),
        l1_verdict(Severity.MEDIUM, 0.6, ThreatType.PROMPT_INJECTION),
        l1_verdict(Severity.HIGH, 0.9, ThreatType.PROMPT_INJECTION),
        l1_verdict(Severity.HIGH, 0.93, ThreatType.DATA_EXFILTRATION),
        l1_verdict(Severity.CRITICAL, 0.98, ThreatType.INSTRUCTION_OVERRIDE),
    ]
    l2s = [
        None,
        SanitizedIntent(sanitized_body="hello"),
        failed_l2(),
        SanitizedIntent(
            sanitized_body="ok",
            stripped_segments=[
                StrippedSegment(
                    text="x" * 40, reason="rule:r", score=0.5, span_start=0, span_end=40
                )
            ],
        ),
    ]
    l3bs = [[], quarantined_chunks(0, 3), quarantined_chunks(1, 3), quarantined_chunks(2, 3)]
    l4s = [
        None,
        OutputVerdict(),
        unsafe_l4(),
        OutputVerdict(severity=Severity.HIGH, score=0.95, complied_with_injected_goal=True),
        OutputVerdict(severity=Severity.MEDIUM, score=0.6, citation_mismatch=True),
    ]
    for l1, l2, l3b, l4 in itertools.product(l1s, l2s, l3bs, l4s):
        yield GuardReport(message_id="m", l1=l1, l2=l2, l3b=l3b, l4=l4)


def test_strictest_wins_and_only_differs_from_first_match_when_a_weaker_rule_used_to_win(engine):
    differing = 0
    total = 0
    for report, stage, category, draft in itertools.product(
        report_space(),
        ["inbound", "outbound"],
        [None, "support", "acknowledgement"],
        [None, "reply", "forward"],
    ):
        facts = engine.facts(report, stage=stage, category=category, draft_action=draft)
        old = legacy_decision(engine, facts)
        new = engine.decide(report, stage=stage, category=category, draft_action=draft)
        total += 1
        old_action = old.action if old is not None else engine.default_action
        old_id = old.id if old is not None else "default"
        if new.matched_rule_id == old_id:
            continue
        differing += 1
        # a different winner means a strictly stricter action than the first match
        assert new.action.rank > old_action.rank, (old_id, new.matched_rule_id)
        assert new.matched_rule_id in new.matched_rule_ids
        assert old_id in new.matched_rule_ids
        # nothing stricter than the winner matched
        rules = {r.id: r for r in engine.rules}
        assert all(rules[i].action.rank <= new.action.rank for i in new.matched_rule_ids)
    assert total > 1000
    assert differing > 0  # the space really does contain the weaker-wins cases


def test_default_rule_and_clean_paths_are_unchanged_across_the_space(engine):
    """Where only P10/P99-style outcomes are possible, decisions are exactly the old ones."""
    for stage, category, draft in itertools.product(
        ["inbound", "outbound"],
        [None, "support", "acknowledgement", "scheduling"],
        [None, "reply", "forward"],
    ):
        report = GuardReport(l4=OutputVerdict())
        facts = engine.facts(report, stage=stage, category=category, draft_action=draft)
        old = legacy_decision(engine, facts)
        new = engine.decide(report, stage=stage, category=category, draft_action=draft)
        assert old is not None
        assert new.matched_rule_id == old.id
        assert new.action == old.action
