"""Context assembly and thread summarization package (Phase 4, R8, R14)."""

from packages.context.assembly import (
    AssembledThreadContext,
    ThreadContextAssembler,
)
from packages.context.policy import (
    THREAD_SUMMARY_SCHEMA,
    SummarizationDecision,
    SummarizationPolicy,
)
from packages.context.summarizer import (
    SummarizationResult,
    ThreadSummarizer,
)

__all__ = [
    "THREAD_SUMMARY_SCHEMA",
    "AssembledThreadContext",
    "SummarizationDecision",
    "SummarizationPolicy",
    "SummarizationResult",
    "ThreadContextAssembler",
    "ThreadSummarizer",
]
