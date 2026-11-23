"""L3 Inbound Gate (ADR-0015 R-c, design decision 9 / F2).

The gate runs the shared ``PolicyEngine`` at stage ``inbound``. The pipeline calls it twice:
pass 1 on the Inbound Guard's facts only (the documents are not scanned yet, so P01/P04 cannot
fire from them); a block or quarantine there stops the job before the Doc Scanner. Otherwise
pass 2 sees every fact. Only the final pass is audited, and its ``audit_id`` seed includes the
pass number, so a replayed job gets the same id and the two paths never share one.
"""

from __future__ import annotations

from mailguard.contracts.policy import GuardReport, PolicyAction, PolicyDecision
from mailguard.contracts.verdict import LayerName
from mailguard.policy.engine import PolicyEngine

BLOCKING = (PolicyAction.BLOCK, PolicyAction.QUARANTINE)


class InboundGate:
    """L3 Inbound Gate over the shared policy engine."""

    name = LayerName.INBOUND_GATE

    def __init__(self, policy: PolicyEngine) -> None:
        self.policy = policy

    def decide(
        self, report: GuardReport, *, category: str | None, gate_pass: int, final: bool
    ) -> PolicyDecision:
        """One gate pass; ``final`` passes are audited, a provisional pass 1 is not."""
        decision = self.policy.decide(
            report, stage="inbound", category=category, gate_pass=gate_pass, audit=final
        )
        decision.metadata["gate_passes"] = gate_pass
        return decision

    @staticmethod
    def stops(decision: PolicyDecision | None) -> bool:
        return decision is not None and decision.action in BLOCKING


__all__ = ["BLOCKING", "InboundGate"]
