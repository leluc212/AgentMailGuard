"""Domain layer: pure entities, value objects, and processing state machine.

Imports standard library and packages/core ONLY.
"""

from packages.domain.entities import (
    AttachmentRef,
    Candidate,
    Checkpoint,
    Classification,
    ContextPackage,
    DraftRef,
    EmailAddress,
    EmailThread,
    Job,
    Mailbox,
    NormalizedMessage,
    OutboundReply,
    ProcessingEvent,
    RawMessage,
    RawThread,
    SentRef,
    Subscription,
    SyncResult,
    ThreadRef,
)
from packages.domain.rules import (
    EmailContext,
    Rule,
    RuleAction,
    RuleEngine,
)
from packages.domain.state_machine import (
    TRANSITIONS,
    IllegalStateTransitionError,
    JobState,
    transition_job,
    validate_transition,
)

__all__ = [
    "TRANSITIONS",
    "AttachmentRef",
    "Candidate",
    "Checkpoint",
    "Classification",
    "ContextPackage",
    "DraftRef",
    "EmailAddress",
    "EmailContext",
    "EmailThread",
    "IllegalStateTransitionError",
    "Job",
    "JobState",
    "Mailbox",
    "NormalizedMessage",
    "OutboundReply",
    "ProcessingEvent",
    "RawMessage",
    "RawThread",
    "Rule",
    "RuleAction",
    "RuleEngine",
    "SentRef",
    "Subscription",
    "SyncResult",
    "ThreadRef",
    "transition_job",
    "validate_transition",
]
