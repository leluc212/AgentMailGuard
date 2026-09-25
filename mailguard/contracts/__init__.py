"""Typed data contracts exchanged between guard layers (the only shared vocabulary)."""

from mailguard.contracts.email import DraftCandidate, GuardedEmail, RetrievedChunk
from mailguard.contracts.policy import GuardReport, PolicyAction, PolicyDecision, RiskTier
from mailguard.contracts.verdict import (
    ChunkVerdict,
    Finding,
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
    "DraftCandidate",
    "Finding",
    "GuardReport",
    "GuardedEmail",
    "LayerName",
    "LayerVerdict",
    "OutputVerdict",
    "PolicyAction",
    "PolicyDecision",
    "Redaction",
    "RetrievedChunk",
    "RiskTier",
    "SanitizedIntent",
    "Severity",
    "StrippedSegment",
    "ThreatType",
]
