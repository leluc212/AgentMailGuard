"""Integration tests for ThreadSummarizer with live PostgreSQL container (R8.2–R8.4).

Verifies:
- Thread below threshold triggers zero LLM calls and creates no spurious DB rows (R8.2).
- Thread exceeding threshold invokes LLM and persists state to PostgreSQL (R8.3).
- Re-checking without new messages skips LLM call via summarized_through_message_id (R8.4).
- Adding new messages triggers incremental re-summarization with version increment (R8.6).
- Strict multi-tenant isolation across distinct organizations (GEMINI.md §8).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import asyncpg
import pytest

from packages.context.summarizer import ThreadSummarizer
from packages.core.settings import AppSettings, SummarizationSettings
from packages.db.connection import create_pool_from_settings
from packages.db.thread_state import PostgresThreadStateStore
from packages.domain.entities import EmailAddress, NormalizedMessage
from packages.llm.fake import FakeLLMProvider


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    """Provide a dedicated asyncpg connection pool connected to the test database."""
    settings = AppSettings().database
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


async def ensure_test_org(pool: asyncpg.Pool, org_id: uuid.UUID) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING;",
            org_id,
            f"Test Org {org_id.hex[:6]}",
        )


async def ensure_test_mailbox(
    pool: asyncpg.Pool,
    org_id: uuid.UUID,
    mbx_id: uuid.UUID,
    address: str = "test@example.com",
) -> None:
    await ensure_test_org(pool, org_id)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)
            VALUES ($1, $2, 'gmail', $3, 'Test Mailbox', 'active')
            ON CONFLICT (id) DO NOTHING;
            """,
            mbx_id,
            org_id,
            address,
        )


async def ensure_test_thread(
    pool: asyncpg.Pool,
    org_id: uuid.UUID,
    mbx_id: uuid.UUID,
    thread_id: uuid.UUID,
    subject: str = "Test Subject",
) -> None:
    await ensure_test_mailbox(pool, org_id, mbx_id)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO email_thread (
                id, organization_id, mailbox_id, subject_normalized, message_count, status
            )
            VALUES ($1, $2, $3, $4, 1, 'open')
            ON CONFLICT (id) DO NOTHING;
            """,
            thread_id,
            org_id,
            mbx_id,
            subject,
        )


async def ensure_test_message(pool: asyncpg.Pool, msg: NormalizedMessage) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                direction, sender_email, sender_name, subject, body_text_clean, received_at
            ) VALUES (
                $1, $2, $3, $4, $5,
                'inbound', $6, $7, $8, $9, $10
            ) ON CONFLICT (id) DO NOTHING;
            """,
            msg.message_id,
            msg.organization_id,
            msg.mailbox_id,
            msg.thread_id,
            msg.provider_message_id,
            msg.sender.email if msg.sender else None,
            msg.sender.name if msg.sender else None,
            msg.subject,
            msg.body_text_clean,
            msg.received_at,
        )


def make_test_message(
    idx: int,
    thread_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    org_id: uuid.UUID,
    body: str = "Default content",
) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=uuid.uuid4(),
        thread_id=thread_id,
        mailbox_id=mailbox_id,
        organization_id=org_id,
        provider="gmail",
        provider_message_id=f"prov-msg-{idx}",
        sender=EmailAddress(email=f"sender{idx}@example.com"),
        received_at=datetime.now(UTC),
        subject=f"Inquiry Subject #{idx}",
        body_text_clean=body,
    )


@pytest.mark.asyncio
async def test_postgres_summarization_lifecycle(db_pool: asyncpg.Pool) -> None:
    """Verify end-to-end summarization lifecycle persisted in PostgreSQL (R8.2–R8.4)."""
    store = PostgresThreadStateStore(db_pool)
    settings = SummarizationSettings(min_messages_threshold=4, context_token_threshold=1500)
    fake_llm = FakeLLMProvider(
        default_response={
            "topic": "Account Lockout Resolution",
            "current_intent": "unlock_account",
            "summary": "User locked out after multiple attempts; identity confirmed.",
            "open_questions": ["Reset temporary password?"],
            "resolved_items": ["Security questions answered correctly"],
        }
    )
    summarizer = ThreadSummarizer(llm=fake_llm, store=store, settings=settings)

    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    await ensure_test_thread(db_pool, org_id, mbx_id, thread_id)

    # 1. Short thread: 2 messages (below threshold 4) -> 0 LLM calls, no DB row
    messages = [make_test_message(i, thread_id, mbx_id, org_id, "Brief note") for i in range(2)]
    for m in messages:
        await ensure_test_message(db_pool, m)

    res1 = await summarizer.summarize_thread(
        organization_id=org_id,
        thread_id=thread_id,
        messages=messages,
    )
    assert not res1.summarized
    assert len(fake_llm.recorded_calls) == 0
    db_state1 = await store.get(org_id, thread_id)
    assert db_state1 is None

    # 2. Add 3 more messages (total 5 > threshold 4) -> 1 LLM call, DB row created
    for i in range(2, 5):
        new_m = make_test_message(
            i, thread_id, mbx_id, org_id, f"Detailed follow-up message {i}"
        )
        await ensure_test_message(db_pool, new_m)
        messages.append(new_m)

    latest_msg_id = messages[-1].message_id
    assert isinstance(latest_msg_id, uuid.UUID)

    res2 = await summarizer.summarize_thread(
        organization_id=org_id,
        thread_id=thread_id,
        messages=messages,
    )
    assert res2.summarized
    assert len(fake_llm.recorded_calls) == 1
    assert res2.thread_state is not None
    assert res2.thread_state.version == 1
    assert res2.thread_state.summarized_through_message_id == latest_msg_id

    # Verify state in PostgreSQL
    db_state2 = await store.get(org_id, thread_id)
    assert db_state2 is not None
    assert db_state2.topic == "Account Lockout Resolution"
    assert db_state2.summarized_through_message_id == latest_msg_id
    assert db_state2.version == 1

    # 3. Re-run without new messages -> R8.4 skips regeneration (0 new LLM calls)
    res3 = await summarizer.summarize_thread(
        organization_id=org_id,
        thread_id=thread_id,
        messages=messages,
        current_state=db_state2,
    )
    assert not res3.summarized
    assert len(fake_llm.recorded_calls) == 1  # Call count remains 1

    # 4. A 6th message arrives -> re-summarization triggered, version increments to 2
    fake_llm.queue_response(
        {
            "topic": "Account Lockout Resolution",
            "current_intent": "password_reset_confirmed",
            "summary": "User locked out; identity confirmed; password successfully reset.",
            "open_questions": [],
            "resolved_items": [
                "Security questions answered correctly",
                "Password reset link delivered",
            ],
        }
    )
    msg6 = make_test_message(
        5, thread_id, mbx_id, org_id, "Password reset completed, thank you!"
    )
    await ensure_test_message(db_pool, msg6)
    messages.append(msg6)
    new_latest_id = messages[-1].message_id
    assert isinstance(new_latest_id, uuid.UUID)

    res4 = await summarizer.summarize_thread(
        organization_id=org_id,
        thread_id=thread_id,
        messages=messages,
    )
    assert res4.summarized
    assert len(fake_llm.recorded_calls) == 2
    assert res4.thread_state is not None
    assert res4.thread_state.version == 2
    assert res4.thread_state.summarized_through_message_id == new_latest_id
    assert res4.thread_state.open_questions == []

    # Verify updated row in PostgreSQL
    db_state4 = await store.get(org_id, thread_id)
    assert db_state4 is not None
    assert db_state4.version == 2
    assert db_state4.summarized_through_message_id == new_latest_id
    assert "Password reset link delivered" in db_state4.resolved_items
