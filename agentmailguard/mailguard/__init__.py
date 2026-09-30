"""AgentMailGuard — multi-layer prompt-injection defence for RAG-based email assistants.

Layer map (see docs/architecture.md):

    L1  Email Injection Scanner      inbound email  -> LayerVerdict (injection)
    L2  User Intent Extractor        inbound email  -> SanitizedIntent
    L3  Channel Isolation Layer      prompt build   -> SecurePrompt (spotlighting)
    L3b Retrieved Document Scanner   RAG chunks     -> ChunkVerdict[] (quarantine)
    L4  Output Scanner               LLM draft      -> OutputVerdict (redaction)
    L5  Email Policy Engine          all verdicts   -> PolicyDecision (gating)

The package is a *cross-cutting subsystem*: it never imports the rag-email core.
Integration with the core happens only through duck-typed adapters in
``mailguard.integration`` and the ``GuardedLLMProvider`` wrapper (L3).
"""

from mailguard.contracts.email import GuardedEmail, RetrievedChunk
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
    "RetrievedChunk",
    "RiskTier",
    "Severity",
    "ThreatType",
    "__version__",
]
