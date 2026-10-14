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

from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class LayerName(StrEnum):
    """Stable identifiers of the four v3 guard layers (ADR-0015).

    The "L1".."L4" labels appear only in docs and reports. Channel isolation builds the
    prompt and is not a layer: it emits ``IsolationInfo``, never a verdict (R-d).
    """

    INBOUND_GUARD = "inbound_guard"  # L1: scan + extract
    DOC_SCANNER = "doc_scanner"  # L2: retrieved documents
    INBOUND_GATE = "inbound_gate"  # L3: policy before any model call
    OUTPUT_GUARD = "output_guard"  # L4: draft scan + outbound policy


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


# --------------------------------------------------------------------------- L1 Inbound Guard
class StrippedSegment(BaseModel):
    """A text segment removed from the email body because it carried instructions."""

    text: str
    reason: str
    score: float = Field(ge=0.0, le=1.0)
    span_start: int
    span_end: int


class SanitizedIntent(LayerVerdict):
    """Result of the extract sub-stage of the Inbound Guard (strip + intent).

    The report never carries it directly: ``InboundVerdict.merge`` folds it, with the scan
    sub-stage's verdict, into the one ``InboundVerdict`` of the email.
    """

    layer: LayerName = LayerName.INBOUND_GUARD
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


InboundStage = Literal["scan", "extract"]


def _tag_stage(findings: Sequence[Finding], stage: str) -> list[Finding]:
    return [f.model_copy(update={"metadata": {**f.metadata, "stage": stage}}) for f in findings]


def _stage_record(verdict: LayerVerdict) -> dict[str, Any]:
    """One sub-stage record of ``InboundVerdict.metadata["stages"]`` (findings are top-level)."""
    return {
        "severity": str(verdict.severity),
        "score": verdict.score,
        "decided_by": verdict.decided_by,
        "model": verdict.model,
        "latency_ms": verdict.latency_ms,
        "error": verdict.error,
        "metadata": dict(verdict.metadata),
    }


class InboundVerdict(SanitizedIntent):
    """L1 Inbound Guard verdict: the scan and extract sub-stages of one email, merged.

    ``metadata["stages"]`` holds one record per sub-stage that ran (``scan``, ``extract``):
    its own severity, score, decided_by, model, latency, error and metadata. Every finding
    carries ``metadata["stage"]``. ``question_sentences`` is recorded by the pipeline
    (``prompting/questions.py``, Task 5), never computed here.
    """

    question_sentences: list[str] = Field(default_factory=list)

    def stage(self, name: InboundStage) -> dict[str, Any] | None:
        stages = self.metadata.get("stages") or {}
        record = stages.get(name)
        return record if isinstance(record, dict) else None

    @classmethod
    def merge(
        cls, scan: LayerVerdict | None, extract: SanitizedIntent | None
    ) -> InboundVerdict | None:
        """Fold the two sub-stage verdicts into one (pure; ``None`` when neither ran).

        ADR-0015 decision 8 (R-i): severity, score, decided_by and model come from ``scan``;
        without a scan the email is ungraded (``none``). The extractor's findings join the
        union (tagged ``extract``) but never raise the severity. Intent, sanitized body and
        stripped segments come from ``extract``; the error is set when either sub-stage
        errored; latency is the sum. (The F11 transitional rule was removed in Task 6: the
        Inbound Guard runs both sub-stages or neither.)
        """
        parts: list[tuple[str, LayerVerdict]] = [
            (name, v) for name, v in (("scan", scan), ("extract", extract)) if v is not None
        ]
        if not parts:
            return None
        grader: LayerVerdict = (
            scan
            if scan is not None
            else LayerVerdict(layer=LayerName.INBOUND_GUARD, decided_by="none")
        )
        findings: list[Finding] = []
        for name, v in parts:
            findings += _tag_stage(v.findings, name)
        errors = [f"{name}: {v.error}" for name, v in parts if v.error]
        intent_fields: dict[str, Any] = {}
        if extract is not None:
            intent_fields = {
                "sanitized_body": extract.sanitized_body,
                "user_intent": extract.user_intent,
                "requested_actions": list(extract.requested_actions),
                "entities": {k: list(v) for k, v in extract.entities.items()},
                "stripped_segments": list(extract.stripped_segments),
                "confidence": extract.confidence,
            }
        return cls(
            severity=grader.severity,
            score=grader.score,
            findings=findings,
            decided_by=grader.decided_by,
            model=grader.model,
            latency_ms=sum(v.latency_ms for _, v in parts),
            error="; ".join(errors) or None,
            metadata={"stages": {name: _stage_record(v) for name, v in parts}},
            **intent_fields,
        )


# --------------------------------------------------------------------------- L2 Doc Scanner
ChunkTrust = Literal["trusted", "untrusted", "quarantined"]


class ChunkVerdict(LayerVerdict):
    """Per-chunk verdict of the Doc Scanner."""

    layer: LayerName = LayerName.DOC_SCANNER
    chunk_id: str = ""
    document_id: str = ""
    quarantined: bool = False
    trust: ChunkTrust = "untrusted"
    cluster_id: str | None = None
    signals: dict[str, float] = Field(default_factory=dict)


class DocumentVerdict(LayerVerdict):
    """Doc Scanner verdict over the whole retrieved set (one ``ChunkVerdict`` per chunk)."""

    layer: LayerName = LayerName.DOC_SCANNER
    chunks: list[ChunkVerdict] = Field(default_factory=list)
    kept_chunk_ids: list[str] = Field(default_factory=list)
    quarantined_chunk_ids: list[str] = Field(default_factory=list)
    quarantine_ratio: float = Field(default=0.0, ge=0.0, le=1.0)

    @classmethod
    def from_chunks(cls, verdicts: Sequence[ChunkVerdict]) -> DocumentVerdict:
        """Aggregate per-chunk verdicts (pure): the strictest chunk sets severity and score."""
        chunks = list(verdicts)
        quarantined = [c.chunk_id for c in chunks if c.quarantined]
        top = max(chunks, key=lambda c: (c.severity.rank, c.score), default=None)
        errors = [f"{c.chunk_id}: {c.error}" for c in chunks if c.error]
        return cls(
            severity=top.severity if top is not None else Severity.NONE,
            score=top.score if top is not None else 0.0,
            findings=[f for c in chunks for f in c.findings],
            decided_by=top.decided_by if top is not None else "rule",
            model=top.model if top is not None else None,
            latency_ms=sum(c.latency_ms for c in chunks),
            error="; ".join(errors) or None,
            chunks=chunks,
            kept_chunk_ids=[c.chunk_id for c in chunks if not c.quarantined],
            quarantined_chunk_ids=quarantined,
            quarantine_ratio=(len(quarantined) / len(chunks)) if chunks else 0.0,
        )


# --------------------------------------------------------------------------- channel isolation
class IsolationInfo(BaseModel):
    """What channel isolation did while building the prompt. Not a verdict (R-d).

    ``forged_markers`` holds one ``forged_channel_marker`` finding (``metadata["stage"] =
    "isolation"``) per marker scrubbed from thread history; markers in the email or the
    documents are the Inbound Guard's and the Doc Scanner's to grade. Isolation runs after
    the inbound gate, so none of this can change the inbound decision.
    """

    enabled: bool = True
    mode: str = "none"
    nonce: str = ""
    chunks_in: int = 0
    chunks_kept: int = 0
    markers_scrubbed: int = 0
    template_tokens_scrubbed: int = 0
    forged_markers: list[Finding] = Field(default_factory=list)
    truncated: bool = False
    error: str | None = None
    latency_ms: int = 0


# --------------------------------------------------------------------------- L4 Output Guard
class Redaction(BaseModel):
    kind: str = Field(description="pii.email | pii.phone | secret.aws_key | ...")
    replacement: str
    span_start: int
    span_end: int
    original_hash: str = Field(description="sha256 of the redacted value, for audit")


class OutputVerdict(LayerVerdict):
    """Output Guard verdict, including the redacted draft."""

    layer: LayerName = LayerName.OUTPUT_GUARD
    original_text: str = ""
    redacted_text: str = ""
    redactions: list[Redaction] = Field(default_factory=list)
    citation_mismatch: bool = False
    unknown_citations: list[str] = Field(default_factory=list)
    complied_with_injected_goal: bool = False
