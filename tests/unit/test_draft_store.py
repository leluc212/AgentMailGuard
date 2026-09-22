"""Unit tests for DraftStore (InMemoryDraftStore) (R5.2, R16.4, R16.6)."""

from uuid import uuid4

import pytest

from packages.db.draft import InMemoryDraftStore
from packages.domain.entities import GeneratedDraft


@pytest.mark.asyncio
async def test_in_memory_draft_store_crud() -> None:
    store = InMemoryDraftStore()
    org_id = uuid4()
    job_id = uuid4()
    msg_id = uuid4()
    th_id = uuid4()
    draft_id = uuid4()

    draft = GeneratedDraft(
        id=draft_id,
        organization_id=org_id,
        job_id=job_id,
        message_id=msg_id,
        thread_id=th_id,
        action="reply",
        subject="Re: Welcome",
        body="Thank you for signing up!",
        confidence=0.99,
        citations=[],
        citation_mismatch=False,
        model_name="template",
        model_tier="template",
        prompt_version="ack-receipt-v1",
        input_tokens=0,
        output_tokens=0,
        cost_estimate=0.0,
        status="draft",
    )

    created = await store.create_draft(draft)
    assert created.id == draft_id
    assert created.organization_id == org_id
    assert created.body == "Thank you for signing up!"

    # Get by ID
    fetched = await store.get_draft(draft_id, org_id)
    assert fetched is not None
    assert fetched.id == draft_id
    assert fetched.subject == "Re: Welcome"

    # Cross-tenant get returns None
    other_org = uuid4()
    cross_org = await store.get_draft(draft_id, other_org)
    assert cross_org is None

    # List by job
    by_job = await store.list_drafts_for_job(job_id, org_id)
    assert len(by_job) == 1
    assert by_job[0].id == draft_id

    # List by thread
    by_thread = await store.list_drafts_for_thread(th_id, org_id)
    assert len(by_thread) == 1
    assert by_thread[0].id == draft_id
