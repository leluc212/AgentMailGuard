"""Draft review decisions and character edit distance (task 6.2; R16.7, R21.4)."""

from __future__ import annotations

import pytest

from packages.domain.review import (
    DraftStatus,
    FeedbackDecision,
    ReviewVerdict,
    approval_verdict,
    edit_distance,
    rejection_verdict,
)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("", "", 0),
        ("abc", "abc", 0),
        ("kitten", "sitting", 3),
        ("", "abc", 3),
        ("abc", "", 3),
        ("flaw", "lawn", 2),
        ("café", "cafe", 1),
        ("Order shipped.", "Order shipped today.", 6),
    ],
)
def test_edit_distance_is_character_levenshtein(a: str, b: str, expected: int) -> None:
    assert edit_distance(a, b) == expected
    assert edit_distance(b, a) == expected


def test_edit_distance_on_long_bodies_with_a_small_edit_is_exact() -> None:
    """Common prefix/suffix trimming keeps a one-character edit in a 10k body cheap."""
    original = "a" * 5000 + "x" + "b" * 5000
    edited = "a" * 5000 + "y" + "b" * 5000
    assert edit_distance(original, edited) == 1


def test_unchanged_approval_is_accepted() -> None:
    assert approval_verdict("Hello Alice", "Hello Alice") == ReviewVerdict(
        FeedbackDecision.ACCEPTED, None, 0
    )


def test_changed_approval_is_edited_with_body_and_distance() -> None:
    verdict = approval_verdict("Hello Alice", "Hello Alice!")
    assert verdict.decision is FeedbackDecision.EDITED
    assert verdict.edited_body == "Hello Alice!"
    assert verdict.edit_distance == 1


def test_rejection_keeps_edits_when_there_were_any() -> None:
    assert rejection_verdict("Hi", "Hi") == ReviewVerdict(FeedbackDecision.REJECTED, None, 0)
    edited = rejection_verdict("Hi", "Hey")
    assert edited.decision is FeedbackDecision.REJECTED
    assert edited.edited_body == "Hey"
    assert edited.edit_distance == 2


def test_status_and_decision_values_match_the_schema_checks() -> None:
    """Values are the migration 0001 CHECK constraint literals."""
    assert [s.value for s in DraftStatus] == ["draft", "approved", "rejected", "dispatched"]
    assert [d.value for d in FeedbackDecision] == ["accepted", "edited", "rejected"]
