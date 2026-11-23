"""AgentMailGuard — multi-layer prompt-injection defence for RAG-based email assistants.

Layer map, schema 3 (ADR-0015; see docs/architecture.md):

    L1  Inbound Guard (scan + extract)   inbound email   -> InboundVerdict
    L2  Doc Scanner                      retrieved docs  -> DocumentVerdict (ChunkVerdict each)
    L3  Inbound Gate                     L1 + L2 facts   -> PolicyDecision (before generation)
    --  Channel Isolation (not a layer)  prompt build    -> SecurePrompt + IsolationInfo
    L4  Output Guard                     LLM draft       -> OutputVerdict + PolicyDecision

The package is a *cross-cutting subsystem*: it never imports the rag-email core.
Integration with the core happens only through duck-typed adapters in
``mailguard.integration`` and the ``GuardedLLMProvider`` wrapper (channel isolation).
"""

from mailguard.contracts.email import GuardedEmail, SourceDocument
from mailguard.contracts.policy import GuardReport, PolicyAction, PolicyDecision, RiskTier
from mailguard.contracts.verdict import Finding, LayerName, LayerVerdict, Severity, ThreatType

__version__ = "0.1.0"

__all__ = [
    "Finding",
    "GuardReport",
    "GuardedEmail",
    "LayerName",
    "LayerVerdict",
    "PolicyAction",
    "PolicyDecision",
    "RiskTier",
    "Severity",
    "SourceDocument",
    "ThreatType",
    "__version__",
]
