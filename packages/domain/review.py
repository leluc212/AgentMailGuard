"""Draft review decisions: draft status, feedback decision and character edit distance.

Requirements:
- R16.6: a draft moves draft -> approved | rejected -> dispatched (design.md §5.8).
- R16.7: every decision is one ``feedback`` row with decision, edited body and edit distance.
- R21.4 / SC3: ``draft_decisions_total{decision, category}`` counts these decisions.

Pure: standard library only (GEMINI.md; tests/unit/test_dependency_rules.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DraftStatus(StrEnum):
    """``generated_draft.status`` values (migration 0001 CHECK constraint)."""

    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISPATCHED = "dispatched"


class FeedbackDecision(StrEnum):
    """``feedback.decision`` values; ``accepted`` means approved unchanged (design.md §5.8)."""

    ACCEPTED = "accepted"
    EDITED = "edited"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ReviewVerdict:
    """What one reviewer decision records in its ``feedback`` row."""

    decision: FeedbackDecision
    edited_body: str | None
    edit_distance: int


def edit_distance(a: str, b: str) -> int:
    """Character-level Levenshtein distance (insert, delete, substitute each cost 1).

    The common prefix and suffix are trimmed first, so the usual review edit (a few
    characters in a long body) costs little; the remaining core is the classic two-row DP.
    """
    if a == b:
        return 0
    start = 0
    limit = min(len(a), len(b))
    while start < limit and a[start] == b[start]:
        start += 1
    end_a, end_b = len(a), len(b)
    while end_a > start and end_b > start and a[end_a - 1] == b[end_b - 1]:
        end_a -= 1
        end_b -= 1
    core_a, core_b = a[start:end_a], b[start:end_b]
    if not core_a:
        return len(core_b)
    if not core_b:
        return len(core_a)
    if len(core_a) < len(core_b):
        core_a, core_b = core_b, core_a
    previous = list(range(len(core_b) + 1))
    for i, char_a in enumerate(core_a, start=1):
        current = [i]
        for j, char_b in enumerate(core_b, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (char_a != char_b),
                )
            )
        previous = current
    return previous[-1]


def approval_verdict(original_body: str, final_body: str) -> ReviewVerdict:
    """Approve: ``accepted`` when the body is the generated one, else ``edited``."""
    distance = edit_distance(original_body, final_body)
    if distance == 0:
        return ReviewVerdict(FeedbackDecision.ACCEPTED, None, 0)
    return ReviewVerdict(FeedbackDecision.EDITED, final_body, distance)


def rejection_verdict(original_body: str, final_body: str) -> ReviewVerdict:
    """Reject: always ``rejected``; edits made before rejecting are kept for analysis."""
    distance = edit_distance(original_body, final_body)
    return ReviewVerdict(FeedbackDecision.REJECTED, final_body if distance else None, distance)
