"""Duck-typed adapters for embedding MailGuard in the rag-email core."""

from mailguard.integration.adapters import (
    GuardedReplyAgent,
    chunks_from_context,
    decision_to_job_result,
    dispatch_allowed,
    guarded_email_from_context,
)

__all__ = [
    "GuardedReplyAgent",
    "chunks_from_context",
    "decision_to_job_result",
    "dispatch_allowed",
    "guarded_email_from_context",
]
