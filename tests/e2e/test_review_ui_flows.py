"""Approve and edit flows in a real browser (task 6.8; R23.4, R16.6, R16.7).

Chromium drives the real review UI, which calls the real /v1 drafts API backed by the
isolated rag_email_test database (tests/e2e/review_stack.py).
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from packages.core.settings import AppSettings
from tests.e2e.review_stack import (
    SUBJECT,
    THREAD_SUMMARY,
    ReviewStack,
    SeededDraft,
    fetch_review_outcome,
    seed_pending_draft,
)

EDITED_BODY = (
    "Your order ORD-82915 left our warehouse on 26 September; the courier expects to "
    "deliver it tomorrow."
)


def _open_draft_from_queue(page: Page, stack: ReviewStack) -> SeededDraft:
    seeded = stack.run(seed_pending_draft(stack.db, stack.organization_id))
    page.goto(f"{stack.url}/drafts")
    page.get_by_role("link", name=SUBJECT).click()
    expect(page.get_by_role("heading", level=1)).to_have_text(SUBJECT)
    return seeded


def test_approve_from_the_queue_records_accepted_feedback_and_publishes_dispatch(
    page: Page, stack: ReviewStack
) -> None:
    seeded = _open_draft_from_queue(page, stack)
    expect(page.get_by_text(THREAD_SUMMARY)).to_be_visible()
    expect(page.get_by_text("Hello, where is my order ORD-82915? Thanks, Alice")).to_be_visible()

    page.get_by_role("button", name="Approve").click()

    expect(page.get_by_role("status")).to_have_text("Draft approved.")
    expect(page.get_by_role("button", name="Approve")).to_have_count(0)
    outcome = stack.run(fetch_review_outcome(stack.db, stack.organization_id, seeded.draft_id))
    assert outcome.draft_status == "approved"
    assert len(outcome.feedback) == 1
    row = outcome.feedback[0]
    assert row["decision"] == "accepted"
    assert row["edited_body"] is None
    assert row["review_ms"] is not None and row["review_ms"] > 0
    dispatch_queue = AppSettings().broker.queue_dispatch
    assert [(key, env.job_id) for _, key, env in stack.publisher.published] == [
        (dispatch_queue, str(seeded.job_id))
    ]


def test_edit_then_approve_records_edited_feedback_with_the_new_body(
    page: Page, stack: ReviewStack
) -> None:
    seeded = _open_draft_from_queue(page, stack)

    page.get_by_label("Reply text").fill(EDITED_BODY)
    page.get_by_role("button", name="Approve").click()

    expect(page.get_by_role("status")).to_have_text("Draft approved.")
    outcome = stack.run(fetch_review_outcome(stack.db, stack.organization_id, seeded.draft_id))
    assert outcome.draft_status == "approved"
    assert outcome.draft_body == EDITED_BODY
    assert len(outcome.feedback) == 1
    row = outcome.feedback[0]
    assert row["decision"] == "edited"
    assert row["edited_body"] == EDITED_BODY
    assert row["edit_distance"] is not None and row["edit_distance"] > 0
    assert row["review_ms"] is not None and row["review_ms"] > 0
    assert len(stack.publisher.published) == 1


def test_save_changes_persists_the_edit_without_a_decision(page: Page, stack: ReviewStack) -> None:
    seeded = _open_draft_from_queue(page, stack)

    page.get_by_label("Reply text").fill(EDITED_BODY)
    page.get_by_role("button", name="Save changes").click()
    expect(page.get_by_role("status")).to_have_text("Changes saved.")
    page.reload()

    expect(page.get_by_label("Reply text")).to_have_value(EDITED_BODY)
    outcome = stack.run(fetch_review_outcome(stack.db, stack.organization_id, seeded.draft_id))
    assert outcome.draft_status == "draft"
    assert outcome.feedback == []
    assert stack.publisher.published == []


def test_reject_with_a_reason_records_rejected_feedback_and_publishes_nothing(
    page: Page, stack: ReviewStack
) -> None:
    seeded = _open_draft_from_queue(page, stack)

    page.get_by_label("Reason for rejecting (optional)").fill("Wrong order number.")
    page.get_by_role("button", name="Reject").click()

    expect(page.get_by_role("status")).to_have_text("Draft rejected.")
    outcome = stack.run(fetch_review_outcome(stack.db, stack.organization_id, seeded.draft_id))
    assert outcome.draft_status == "rejected"
    assert [r["decision"] for r in outcome.feedback] == ["rejected"]
    assert stack.publisher.published == []
