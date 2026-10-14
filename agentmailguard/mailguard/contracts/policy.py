"""Policy contracts: the final gating decision and the aggregated guard report."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from mailguard.contracts.verdict import (
    DocumentVerdict,
    InboundVerdict,
    IsolationInfo,
    LayerVerdict,
    OutputVerdict,
    Severity,
)


class RiskTier(StrEnum):
    """Aggregated risk tier computed by the policy engine."""

    T0_CLEAN = "t0_clean"
    T1_LOW = "t1_low"
    T2_MEDIUM = "t2_medium"
    T3_HIGH = "t3_high"
    T4_CRITICAL = "t4_critical"

    @classmethod
    def from_severity(cls, severity: Severity) -> RiskTier:
        return {
            Severity.NONE: cls.T0_CLEAN,
            Severity.LOW: cls.T1_LOW,
            Severity.MEDIUM: cls.T2_MEDIUM,
            Severity.HIGH: cls.T3_HIGH,
            Severity.CRITICAL: cls.T4_CRITICAL,
        }[severity]

    @property
    def rank(self) -> int:
        return list(RiskTier).index(self)


class PolicyAction(StrEnum):
    """Risk-tiered actions enforced at the Draft Store & Dispatcher boundary."""

    AUTO_SEND = "auto_send"  # send without a human (opt-in per category only)
    DRAFT_ONLY = "draft_only"  # create provider draft, reviewer decides (default posture)
    HUMAN_APPROVAL = "human_approval"  # draft + mandatory security reviewer sign-off
    BLOCK = "block"  # do not generate / do not dispatch; notify operator
    QUARANTINE = "quarantine"  # block + isolate message & chunks for forensics

    @property
    def rank(self) -> int:
        return _ACTION_RANK[self]

    @classmethod
    def strictest(cls, *actions: PolicyAction) -> PolicyAction:
        return max(actions, key=lambda a: a.rank, default=cls.DRAFT_ONLY)


_ACTION_RANK: dict[PolicyAction, int] = {
    PolicyAction.AUTO_SEND: 0,
    PolicyAction.DRAFT_ONLY: 1,
    PolicyAction.HUMAN_APPROVAL: 2,
    PolicyAction.BLOCK: 3,
    PolicyAction.QUARANTINE: 4,
}


class PolicyDecision(BaseModel):
    """Deterministic, idempotent, auditable gating decision (L5)."""

    audit_id: str = Field(default_factory=lambda: uuid4().hex)
    action: PolicyAction
    risk_tier: RiskTier
    matched_rule_id: str  # the winning rule: strictest action among all matches
    matched_rule_ids: list[str] = Field(
        default_factory=list, description="Every rule that matched (winner included), by priority"
    )
    reasons: list[str] = Field(default_factory=list)
    requires_human: bool = False
    redactions_applied: int = 0
    quarantined_chunk_ids: list[str] = Field(default_factory=list)
    policy_version: str = "unknown"
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class GuardReport(BaseModel):
    """Everything the guard knows about one email job, in pipeline order (schema 3).

    ``extra="forbid"``: a v2 dump (``l1``, ``l2``, ``l3``, ``l3b``, ``l4``) does not validate.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[3] = 3
    message_id: str = ""
    organization_id: str = ""
    inbound_guard: InboundVerdict | None = None
    doc_scanner: DocumentVerdict | None = None
    isolation: IsolationInfo | None = None
    inbound_decision: PolicyDecision | None = Field(
        default=None, description="Inbound gate decision, taken before generation"
    )
    output_guard: OutputVerdict | None = None
    decision: PolicyDecision | None = Field(
        default=None, description="Latest (final) gate decision"
    )
    total_latency_ms: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    def verdicts(self) -> list[LayerVerdict]:
        """The layer verdicts present, in pipeline order (isolation has none)."""
        return [
            v for v in (self.inbound_guard, self.doc_scanner, self.output_guard) if v is not None
        ]

    @property
    def quarantined_chunk_ids(self) -> list[str]:
        return list(self.doc_scanner.quarantined_chunk_ids) if self.doc_scanner else []

    @property
    def max_severity(self) -> Severity:
        return Severity.max(*(v.severity for v in self.verdicts()))
