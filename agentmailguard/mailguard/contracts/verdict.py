"""Verdict contracts emitted by every guard layer.

Design rules
------------
* Every layer returns a ``LayerVerdict`` (or a subclass) so the policy engine can
  reason over a uniform structure.
* ``score`` is always a calibrated probability-like value in [0, 1]. ``severity`` is
  the *discretised* view used by policy rules. Layers must set both.
* ``Finding`` carries provenance: which detector (rule / ml / llm), which rule id,
  and the character span in the *original* text so the UI can highlight it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class LayerName(StrEnum):
    """Stable identifiers of the six guard layers."""

    L1_INJECTION_SCANNER = "l1_injection_scanner"
    L2_INTENT_EXTRACTOR = "l2_intent_extractor"
    L3_CHANNEL_ISOLATION = "l3_channel_isolation"
    L3B_DOCUMENT_SCANNER = "l3b_document_scanner"
    L4_OUTPUT_SCANNER = "l4_output_scanner"
    L5_POLICY_ENGINE = "l5_policy_engine"


class Severity(StrEnum):
    """Discrete severity ladder. Ordering is meaningful (see ``rank``)."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    @classmethod
    def from_score(cls, score: float, *, flag: float = 0.50, block: float = 0.85) -> Severity:
        """Map a [0,1] score onto the ladder using two thresholds."""
        if score >= 0.97:
            return cls.CRITICAL
        if score >= block:
            return cls.HIGH
        if score >= flag:
            return cls.MEDIUM
        if score >= 0.20:
            return cls.LOW
        return cls.NONE

    @classmethod
    def max(cls, *values: Severity) -> Severity:
        return max(values, key=lambda s: s.rank, default=cls.NONE)


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.NONE: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class ThreatType(StrEnum):
    """Threat taxonomy used across layers (aligned with OWASP Top-10 for LLM Apps 2025)."""

    # Inbound / indirect prompt injection (OWASP LLM01)
    PROMPT_INJECTION = "prompt_injection"
    JAILBREAK = "jailbreak"
    INSTRUCTION_OVERRIDE = "instruction_override"
    ROLE_HIJACK = "role_hijack"
    DATA_EXFILTRATION = "data_exfiltration"
    TOOL_ABUSE = "tool_abuse"
    OBFUSCATION = "obfuscation"
    PHISHING_LURE = "phishing_lure"
    # Retrieval / RAG (OWASP LLM08 vector & embedding weaknesses)
    RAG_POISONING = "rag_poisoning"
    # Outbound (OWASP LLM02 sensitive information disclosure, LLM05 improper output)
    PII_LEAK = "pii_leak"
    SECRET_LEAK = "secret_leak"
    SYSTEM_PROMPT_LEAK = "system_prompt_leak"
    CONTEXT_LEAK = "context_leak"
    CITATION_HALLUCINATION = "citation_hallucination"
    INJECTED_GOAL_COMPLIANCE = "injected_goal_compliance"
    UNSAFE_ACTION = "unsafe_action"
    # Meta
    INTERNAL_ERROR = "internal_error"


class Finding(BaseModel):
    """One concrete piece of evidence produced by a detector."""

    layer: LayerName
    threat_type: ThreatType
    severity: Severity
    score: float = Field(ge=0.0, le=1.0)
    detector: str = Field(description="rule | ml | llm | heuristic")
    rule_id: str | None = None
    technique: str | None = Field(default=None, description="Attack technique tag (taxonomy)")
    span_start: int | None = None
    span_end: int | None = None
    excerpt: str = Field(default="", max_length=400)
    rationale: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class LayerVerdict(BaseModel):
    """Uniform output of a guard layer."""

    layer: LayerName
    severity: Severity = Severity.NONE
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    findings: list[Finding] = Field(default_factory=list)
    decided_by: str = Field(default="rule", description="Highest-cost detector that decided")
    model: str | None = None
    latency_ms: int = 0
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_clean(self) -> bool:
        return self.severity in (Severity.NONE, Severity.LOW) and self.error is None

    def techniques(self) -> list[str]:
        return sorted({f.technique for f in self.findings if f.technique})

    def threat_types(self) -> list[ThreatType]:
        return sorted({f.threat_type for f in self.findings}, key=str)


# --------------------------------------------------------------------------- L2
class StrippedSegment(BaseModel):
    """A text segment removed from the email body because it carried instructions."""

    text: str
    reason: str
    score: float = Field(ge=0.0, le=1.0)
    span_start: int
    span_end: int


class SanitizedIntent(LayerVerdict):
    """Output of the User Intent Extractor (L2)."""

    layer: LayerName = LayerName.L2_INTENT_EXTRACTOR
    sanitized_body: str = ""
    user_intent: str = Field(default="", description="One-paragraph neutral paraphrase")
    requested_actions: list[str] = Field(default_factory=list)
    entities: dict[str, list[str]] = Field(default_factory=dict)
    stripped_segments: list[StrippedSegment] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @property
    def removed_ratio(self) -> float:
        removed = sum(len(s.text) for s in self.stripped_segments)
        total = removed + len(self.sanitized_body)
        return (removed / total) if total else 0.0


# --------------------------------------------------------------------------- L3b
class ChunkVerdict(LayerVerdict):
    """Per-chunk verdict from the Retrieved Document Scanner."""

    layer: LayerName = LayerName.L3B_DOCUMENT_SCANNER
    chunk_id: str = ""
    document_id: str = ""
    quarantined: bool = False


# --------------------------------------------------------------------------- L4
class Redaction(BaseModel):
    kind: str = Field(description="pii.email | pii.phone | secret.aws_key | ...")
    replacement: str
    span_start: int
    span_end: int
    original_hash: str = Field(description="sha256 of the redacted value, for audit")


class OutputVerdict(LayerVerdict):
    """Output Scanner verdict, including the redacted draft."""

    layer: LayerName = LayerName.L4_OUTPUT_SCANNER
    original_text: str = ""
    redacted_text: str = ""
    redactions: list[Redaction] = Field(default_factory=list)
    citation_mismatch: bool = False
    unknown_citations: list[str] = Field(default_factory=list)
    complied_with_injected_goal: bool = False
