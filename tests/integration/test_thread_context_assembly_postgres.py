"""Integration tests for ThreadContextAssembler with live PostgreSQL container (R8.5, R8.7, R8.8).

Verifies:
- Live database retrieval of messages via PostgresMessageStore.
- Live database retrieval of ThreadState via PostgresThreadStateStore.
- R8.5: Assembles context as summary + latest N messages + current email when summary exists.
- R8.7 / H3: Computes and records tokens saved from live thread data.
- Strict multi-tenant isolation across distinct organizations.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from packages.context.assembly import AssembledThreadContext, ThreadContextAssembler
from packages.core.settings import AppSettings, SummarizationSettings
from packages.db.connection import create_pool_from_settings
from packages.db.message import PostgresMessageStore
from packages.db.thread_state import PostgresThreadStateStore
from packages.domain.entities import EmailAddress, NormalizedMessage, ThreadState
from packages.observability.metrics import create_pipeline_metrics


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    """Provide a dedicated asyncpg connection pool connected to the test database."""
    settings = AppSettings().database
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


async def _ensure_test_org(pool: asyncpg.Pool, org_id: uuid.UUID) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING;",
            org_id,
            f"Test Org {org_id.hex[:6]}",
        )


async def _ensure_test_mailbox(
    pool: asyncpg.Pool,
    org_id: uuid.UUID,
    mbx_id: uuid.UUID,
    address: str = "test@example.com",
) -> None:
    await _ensure_test_org(pool, org_id)
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


async def _ensure_test_thread(
    pool: asyncpg.Pool,
    org_id: uuid.UUID,
    mbx_id: uuid.UUID,
    thread_id: uuid.UUID,
    subject: str = "Test Subject",
) -> None:
    await _ensure_test_mailbox(pool, org_id, mbx_id)
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


async def _insert_test_message(pool: asyncpg.Pool, msg: NormalizedMessage) -> None:
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


def _make_msg(
    idx: int,
    thread_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    org_id: uuid.UUID,
    received_at: datetime,
    body: str = "Default content",
) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=uuid.uuid4(),
        thread_id=thread_id,
        mailbox_id=mailbox_id,
        organization_id=org_id,
        provider="gmail",
        provider_message_id=f"prov-msg-{idx}-{uuid.uuid4()}",
        sender=EmailAddress(email=f"sender{idx}@example.com"),
        received_at=received_at,
        subject=f"Inquiry Subject #{idx}",
        body_text_clean=body,
    )


@pytest.mark.asyncio
async def test_thread_context_assembly_live_postgres(db_pool: asyncpg.Pool) -> None:
    """Verifies end-to-end thread context assembly fetching messages and state from PostgreSQL."""
    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    now = datetime.now(UTC)

    await _ensure_test_thread(db_pool, org_id, mbx_id, thread_id)

    # Insert 4 historical messages into PostgreSQL
    hist_messages: list[NormalizedMessage] = []
    for i in range(1, 5):
        m = _make_msg(
            idx=i,
            thread_id=thread_id,
            mailbox_id=mbx_id,
            org_id=org_id,
            received_at=now - timedelta(minutes=20 - i * 4),
            body=(
                f"Historical message {i}: Detailed instructions and requirements. "
                "Ensuring all operational parameters are thoroughly discussed."
            ),
        )
        await _insert_test_message(db_pool, m)
        hist_messages.append(m)

    # Persist ThreadState in PostgreSQL
    state_store = PostgresThreadStateStore(db_pool)
    thread_state = ThreadState(
        thread_id=thread_id,
        organization_id=org_id,
        topic="Account Setup Workflow",
        current_intent="Complete account configuration",
        summary="Customer and support discussed account configuration across messages 1 to 4.",
        open_questions=["Awaiting final approval confirmation"],
        resolved_items=["Credentials validated", "SLA tier selected"],
        summarized_through_message_id=uuid.UUID(str(hist_messages[-1].message_id)),
        version=1,
    )
    await state_store.save(thread_state)

    # Current incoming email (5th message)
    curr_msg = _make_msg(
        idx=5,
        thread_id=thread_id,
        mailbox_id=mbx_id,
        org_id=org_id,
        received_at=now,
        body="Final check: Ready for deployment approval.",
    )
    await _insert_test_message(db_pool, curr_msg)

    # Initialize Assembler with PostgreSQL stores
    msg_store = PostgresMessageStore(db_pool)
    metrics = create_pipeline_metrics()
    assembler = ThreadContextAssembler(
        settings=SummarizationSettings(keep_latest_messages=2),
        metrics=metrics,
        thread_state_store=state_store,
        message_store=msg_store,
    )

    # Assemble without passing thread_messages or thread_state (fetching from DB)
    ctx = await assembler.assemble(
        organization_id=org_id,
        thread_id=thread_id,
        current_message=curr_msg,
    )

    assert isinstance(ctx, AssembledThreadContext)
    assert ctx.has_summary is True
    assert ctx.summary == thread_state.summary
    assert ctx.topic == "Account Setup Workflow"
    assert ctx.current_intent == "Complete account configuration"
    assert ctx.open_questions == ["Awaiting final approval confirmation"]
    assert ctx.resolved_items == ["Credentials validated", "SLA tier selected"]

    # R8.5: latest N messages (default N=2)
    assert len(ctx.recent_messages) == 2
    assert ctx.recent_messages[0].message_id == hist_messages[-2].message_id
    assert ctx.recent_messages[1].message_id == hist_messages[-1].message_id
    assert ctx.current_message.message_id == curr_msg.message_id

    # R8.7 / H3: Token savings
    assert ctx.pre_compression_tokens > ctx.post_compression_tokens
    assert ctx.tokens_saved > 0
    assert 0.0 < ctx.compression_ratio < 1.0


@pytest.mark.asyncio
async def test_thread_context_assembly_tenant_isolation_postgres(db_pool: asyncpg.Pool) -> None:
    """Verifies strict tenant isolation: an organization cannot assemble another's thread state."""
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    mbx_a = uuid.uuid4()
    mbx_b = uuid.uuid4()
    thread_a = uuid.uuid4()
    thread_b = uuid.uuid4()
    now = datetime.now(UTC)

    await _ensure_test_thread(db_pool, org_a, mbx_a, thread_a)
    await _ensure_test_thread(db_pool, org_b, mbx_b, thread_b)

    msg_a = _make_msg(1, thread_a, mbx_a, org_a, now, body="Confidential Org A message")
    await _insert_test_message(db_pool, msg_a)

    state_store = PostgresThreadStateStore(db_pool)
    msg_store = PostgresMessageStore(db_pool)

    await state_store.save(
        ThreadState(
            thread_id=thread_a,
            organization_id=org_a,
            summary="Org A confidential summary",
            version=1,
        )
    )

    assembler = ThreadContextAssembler(
        thread_state_store=state_store,
        message_store=msg_store,
    )

    # Attempting to assemble Org A's thread using Org B's organization_id
    curr_msg_b = _make_msg(2, thread_a, mbx_b, org_b, now, body="Inbound from Org B")
    ctx = await assembler.assemble(
        organization_id=org_b,
        thread_id=thread_a,
        current_message=curr_msg_b,
    )

    # Org B must see NO summary and NO messages from Org A
    assert ctx.has_summary is False
    assert ctx.summary is None
    assert len(ctx.recent_messages) == 0
