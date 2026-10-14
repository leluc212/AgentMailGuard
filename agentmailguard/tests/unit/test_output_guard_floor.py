"""L4 Output Guard: the inbound floor (plan_v3 design decision 1, D1=A, F1) and the decision
the Output Guard owns after generation (R-e: no separate policy step follows L4)."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from mailguard.contracts.email import DraftCandidate
from mailguard.contracts.policy import PolicyAction, PolicyDecision, RiskTier
from mailguard.layers.output_guard.guard import INBOUND_PREFIX, floor_of, merge_floor
from mailguard.pipeline import GuardConfig, MailGuardPipeline

SYSTEM = "You are the Acme support assistant."
CATCH_ALL = frozenset({"default", "P99-default"})
ALLOW = {"acknowledgement", "scheduling"}
SPECIFIC_IN = "P-inbound-specific"
TIER = {
    PolicyAction.AUTO_SEND: RiskTier.T0_CLEAN,
    PolicyAction.DRAFT_ONLY: RiskTier.T1_LOW,
    PolicyAction.HUMAN_APPROVAL: RiskTier.T3_HIGH,
    PolicyAction.BLOCK: RiskTier.T3_HIGH,
    PolicyAction.QUARANTINE: RiskTier.T4_CRITICAL,
}


def _decision(action: PolicyAction, rule: str, stage: str) -> PolicyDecision:
    return PolicyDecision(
        audit_id=f"{stage}-{rule}-{action}",
        action=action,
        risk_tier=TIER[action],
        matched_rule_id=rule,
        matched_rule_ids=[rule],
        reasons=[f"{rule} reason"],
        requires_human=action.rank >= PolicyAction.HUMAN_APPROVAL.rank,
        metadata={"stage": stage},
    )


ROWS = list(
    itertools.product(
        [None, *PolicyAction],  # inbound action (None: inbound gate off)
        [
            PolicyAction.AUTO_SEND,
            PolicyAction.DRAFT_ONLY,
            PolicyAction.HUMAN_APPROVAL,
            PolicyAction.BLOCK,
            PolicyAction.QUARANTINE,
        ],  # fmt: skip
        ["P99-default", SPECIFIC_IN],  # inbound rule: catch-all or specific
        [True, False],  # category allow-listed for auto_send
    )
)


@pytest.mark.parametrize(("inbound_action", "outbound_action", "inbound_rule", "allowed"), ROWS)
def test_the_floor_invariants_hold_for_every_combination(
    inbound_action, outbound_action, inbound_rule, allowed
):
    inbound = None if inbound_action is None else _decision(inbound_action, inbound_rule, "inbound")
    outbound = _decision(outbound_action, "P-outbound", "outbound")
    category = "acknowledgement" if allowed else "support"
    final = merge_floor(
        outbound, inbound, catch_all=CATCH_ALL, auto_send_categories=ALLOW, category=category
    )
    floor = floor_of(inbound, CATCH_ALL)
    expected_floor = (
        inbound is not None
        and inbound_rule not in CATCH_ALL
        and inbound.action.rank > PolicyAction.AUTO_SEND.rank
    )
    assert (floor is not None) is expected_floor
    # (1) a floor is never undercut
    if floor is not None:
        assert final.action.rank >= floor.action.rank
        assert final.action is PolicyAction.strictest(outbound.action, floor.action)
        assert f"{INBOUND_PREFIX}{inbound_rule}" in final.matched_rule_ids
        assert final.risk_tier.rank == max(outbound.risk_tier.rank, floor.risk_tier.rank)
    # (2) auto_send only without a floor and inside the allowlist
    if final.action is PolicyAction.AUTO_SEND:
        assert floor is None and allowed
    if floor is None and not allowed:
        assert final.action is not PolicyAction.AUTO_SEND
    # (3) requires_human is the OR
    assert final.requires_human == (
        outbound.requires_human or (floor is not None and floor.requires_human)
    )
    # winner: the floor only when strictly stricter (a tie goes to the outbound decision)
    carried = floor is not None and floor.action.rank > outbound.action.rank
    assert final.metadata["carried_from_inbound"] is carried
    assert final.metadata["decided_by"] == ("inbound_gate" if carried else "output_guard")
    assert final.matched_rule_id == (inbound_rule if carried else "P-outbound")
    if carried:
        assert final.reasons[0] == f"carried from inbound_gate: {inbound_rule} ({floor.action})"
    # (4) deterministic: the same inputs give the same audit_id
    again = merge_floor(
        outbound, inbound, catch_all=CATCH_ALL, auto_send_categories=ALLOW, category=category
    )
    assert again.audit_id == final.audit_id


def test_a_specific_inbound_auto_send_is_no_floor():
    """Required row (F1): specific inbound rule returning auto_send x outbound auto_send in an
    allow-listed category -> final auto_send, not carried."""
    inbound = _decision(PolicyAction.AUTO_SEND, SPECIFIC_IN, "inbound")
    outbound = _decision(PolicyAction.AUTO_SEND, "P10-clean-auto-send", "outbound")
    final = merge_floor(
        outbound, inbound, catch_all=CATCH_ALL, auto_send_categories=ALLOW,
        category="acknowledgement",
    )  # fmt: skip
    assert final.action is PolicyAction.AUTO_SEND
    assert final.metadata["carried_from_inbound"] is False
    assert final.matched_rule_ids == ["P10-clean-auto-send"]


def test_the_catch_all_set_is_read_from_the_loaded_policy(settings):
    pipe = MailGuardPipeline(settings, GuardConfig.preset("C3"), audit=False)
    assert pipe.output_guard.catch_all_rule_ids() == frozenset({"default", "P99-default"})


# ---------------------------------------------------------------- through the pipeline
async def _reply(_messages):
    return DraftCandidate(body="Your order ORD-1 ships tomorrow.")


def _audit_lines(pipe: MailGuardPipeline) -> list[dict]:
    path = Path(pipe.policy.audit_path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [json.loads(line) for line in lines]


@pytest.fixture
def inbound_only_policy(tmp_path: Path) -> Path:
    """A policy with an inbound-only rule, so the inbound decision is stricter than outbound."""
    path = tmp_path / "policy.yaml"
    path.write_text(
        "version: test-floor\ndefault_action: draft_only\n"
        "auto_send_categories: [acknowledgement]\nrules:\n"
        "  - {id: PX-billing-inbound-review, priority: 5, when: {stage: inbound, "
        "category_in: [billing]}, action: human_approval, requires_human: true, "
        "reason: billing needs review}\n"
        "  - {id: P99-default, priority: 1000, when: {}, action: draft_only}\n",
        encoding="utf-8",
    )
    return path


async def test_the_inbound_floor_is_carried_through_the_pipeline(
    settings, benign_email, inbound_only_policy
):
    settings.policy.policy_path = str(inbound_only_policy)
    pipe = MailGuardPipeline(settings, GuardConfig.preset("C3"))
    report, draft, _ = await pipe.run(
        benign_email, [], _reply, system_instructions=SYSTEM, category="billing"
    )
    assert draft is not None
    assert report.inbound_decision.matched_rule_id == "PX-billing-inbound-review"
    final = report.decision
    assert final.action is PolicyAction.HUMAN_APPROVAL and final.requires_human
    assert final.matched_rule_id == "PX-billing-inbound-review"
    assert final.metadata["carried_from_inbound"] is True
    assert final.metadata["decided_by"] == "inbound_gate"
    assert final.matched_rule_ids == ["P99-default", "inbound:PX-billing-inbound-review"]
    # one audit line per decision; the outbound line is the merged decision
    lines = _audit_lines(pipe)
    assert [line["stage"] for line in lines] == ["inbound", "outbound"]
    assert lines[1]["audit_id"] == final.audit_id and lines[1]["action"] == "human_approval"


async def test_the_merged_audit_id_is_stable_across_identical_runs(
    settings, benign_email, inbound_only_policy
):
    settings.policy.policy_path = str(inbound_only_policy)
    ids = []
    for _ in range(2):
        pipe = MailGuardPipeline(settings, GuardConfig.preset("C3"), audit=False)
        report, _, _ = await pipe.run(
            benign_email, [], _reply, system_instructions=SYSTEM, category="billing"
        )
        ids.append(report.decision.audit_id)
    assert ids[0] == ids[1]


async def test_the_output_guard_decides_without_the_inbound_gate(settings, attack_email):
    """R-e: the outbound policy belongs to the Output Guard, so C3-L3 (no inbound gate) still
    gets a final decision after generation; there is no floor to carry."""
    pipe = MailGuardPipeline(settings, GuardConfig.preset("C3-L3"), audit=False)
    report, draft, _ = await pipe.run(attack_email, [], _reply, system_instructions=SYSTEM)
    assert draft is not None  # nothing stops the job before generation
    assert report.inbound_decision is None
    final = report.decision
    assert final is not None and final.metadata["stage"] == "outbound"
    assert final.metadata["decided_by"] == "output_guard"
    assert final.metadata["carried_from_inbound"] is False
    assert final.action.rank >= PolicyAction.HUMAN_APPROVAL.rank and final.requires_human


async def test_without_the_output_guard_the_inbound_decision_is_final(settings, benign_email):
    pipe = MailGuardPipeline(settings, GuardConfig.preset("C3-L4"), audit=False)
    report, draft, _ = await pipe.run(benign_email, [], _reply, system_instructions=SYSTEM)
    assert draft is not None and report.output_guard is None
    assert report.decision is report.inbound_decision
