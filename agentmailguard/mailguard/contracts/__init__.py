"""Typed data contracts exchanged between guard layers (the only shared vocabulary)."""

from mailguard.contracts.email import DraftCandidate, GuardedEmail, SourceDocument
from mailguard.contracts.policy import GuardReport, PolicyAction, PolicyDecision, RiskTier
from mailguard.contracts.verdict import (
    ChunkVerdict,
    DocumentVerdict,
    Finding,
    InboundVerdict,
    IsolationInfo,
    LayerName,
    LayerVerdict,
    OutputVerdict,
    Redaction,
    SanitizedIntent,
    Severity,
    StrippedSegment,
    ThreatType,
)

__all__ = [
    "ChunkVerdict",
    "DocumentVerdict",
    "DraftCandidate",
    "Finding",
    "GuardReport",
    "GuardedEmail",
    "InboundVerdict",
    "IsolationInfo",
    "LayerName",
    "LayerVerdict",
    "OutputVerdict",
    "PolicyAction",
    "PolicyDecision",
    "Redaction",
    "RiskTier",
    "SanitizedIntent",
    "Severity",
    "SourceDocument",
    "StrippedSegment",
    "ThreatType",
]
