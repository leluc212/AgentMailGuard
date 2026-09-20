"""Integration tests for PostgresThreadStore with live PostgreSQL container.

Requirements:
- R4.5: Normalize subjects by stripping reply/forward prefixes for thread matching.
- R4.6: Associate each message with a thread using provider thread id, In-Reply-To/References,
        or normalized subject + overlapping participant set within time window.
- Multi-tenant verification across >= 3 tenants (GEMINI.md §8).
- Atomic counter increment and array overlap operations.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.thread import PostgresThreadStore
from packages.domain.entities import EmailAddress, EmailThread
from services.email_worker.threading import (
    ThreadAssociationReason,
    ThreadAssociator,
)


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


@pytest.mark.asyncio
async def test_postgres_thread_multi_tenant_isolation(db_pool: asyncpg.Pool) -> None:
    """Verify >= 3 tenants can store overlapping thread IDs with strict tenant isolation."""
    store = PostgresThreadStore(db_pool)

    # Setup 3 distinct tenants
    org_a, mbx_a = uuid.uuid4(), uuid.uuid4()
    org_b, mbx_b = uuid.uuid4(), uuid.uuid4()
    org_c, mbx_c = uuid.uuid4(), uuid.uuid4()

    await ensure_test_mailbox(db_pool, org_a, mbx_a, "user_a@corp.com")
    await ensure_test_mailbox(db_pool, org_b, mbx_b, "user_b@corp.com")
    await ensure_test_mailbox(db_pool, org_c, mbx_c, "user_c@corp.com")

    shared_provider_thread_id = "thread_shared_multi_tenant_01"
    thread_a_id = uuid.uuid4()
    thread_b_id = uuid.uuid4()

    # Tenant A creates thread
    t_a = EmailThread(
        id=thread_a_id,
        organization_id=org_a,
        mailbox_id=mbx_a,
        subject_normalized="Shared Topic Title",
        provider_thread_id=shared_provider_thread_id,
        participants=["alice@partner.com", "user_a@corp.com"],
        message_count=1,
    )
    await store.create_thread(t_a)

    # Tenant B creates thread with IDENTICAL provider_thread_id on Tenant B mailbox
    t_b = EmailThread(
        id=thread_b_id,
        organization_id=org_b,
        mailbox_id=mbx_b,
        subject_normalized="Shared Topic Title",
        provider_thread_id=shared_provider_thread_id,
        participants=["bob@partner.com", "user_b@corp.com"],
        message_count=1,
    )
    await store.create_thread(t_b)

    # Verify lookups are strictly isolated by organization_id
    found_a = await store.find_by_provider_thread_id(org_a, mbx_a, shared_provider_thread_id)
    assert found_a is not None
    assert found_a.id == thread_a_id
    assert found_a.organization_id == org_a

    found_b = await store.find_by_provider_thread_id(org_b, mbx_b, shared_provider_thread_id)
    assert found_b is not None
    assert found_b.id == thread_b_id
    assert found_b.organization_id == org_b

    # Tenant C querying the same ID must see nothing
    found_c = await store.find_by_provider_thread_id(org_c, mbx_c, shared_provider_thread_id)
    assert found_c is None


@pytest.mark.asyncio
async def test_postgres_subject_and_participant_overlap_query(db_pool: asyncpg.Pool) -> None:
    """Verify PostgreSQL array overlap operator (participants && $4) and time window."""
    store = PostgresThreadStore(db_pool)

    org_id, mbx_id = uuid.uuid4(), uuid.uuid4()
    await ensure_test_mailbox(db_pool, org_id, mbx_id, "main@corp.com")

    thread_id = uuid.uuid4()
    now = datetime.now(UTC)

    thread = EmailThread(
        id=thread_id,
        organization_id=org_id,
        mailbox_id=mbx_id,
        subject_normalized="Q4 Contract Negotiation",
        participants=["legal@corp.com", "counsel@vendor.com"],
        first_message_at=now - timedelta(days=2),
        last_message_at=now - timedelta(days=1),
        message_count=1,
    )
    await store.create_thread(thread)

    # 1. Matching subject + overlapping participant ("counsel@vendor.com") within 14 days
    matched = await store.find_by_subject_and_participants(
        organization_id=org_id,
        mailbox_id=mbx_id,
        subject_normalized="q4 contract negotiation",  # Case insensitive
        participants=["counsel@vendor.com", "procurement@corp.com"],
        window=timedelta(days=14),
        reference_time=now,
    )
    assert matched is not None
    assert matched.id == thread_id

    # 2. Same subject but completely disjoint participants -> None
    disjoint = await store.find_by_subject_and_participants(
        organization_id=org_id,
        mailbox_id=mbx_id,
        subject_normalized="Q4 Contract Negotiation",
        participants=["outsider@domain.com"],
        window=timedelta(days=14),
        reference_time=now,
    )
    assert disjoint is None

    # 3. Same subject and overlapping participants but outside 1-hour window -> None
    expired = await store.find_by_subject_and_participants(
        organization_id=org_id,
        mailbox_id=mbx_id,
        subject_normalized="Q4 Contract Negotiation",
        participants=["legal@corp.com"],
        window=timedelta(hours=1),
        reference_time=now,
    )
    assert expired is None


@pytest.mark.asyncio
async def test_postgres_atomic_thread_counter_update(db_pool: asyncpg.Pool) -> None:
    """Verify atomic counter increment, timestamp progression, and array union."""
    store = PostgresThreadStore(db_pool)

    org_id, mbx_id = uuid.uuid4(), uuid.uuid4()
    await ensure_test_mailbox(db_pool, org_id, mbx_id, "desk@corp.com")

    thread_id = uuid.uuid4()
    t0 = datetime.now(UTC) - timedelta(hours=2)

    initial_thread = EmailThread(
        id=thread_id,
        organization_id=org_id,
        mailbox_id=mbx_id,
        subject_normalized="Server Crash Report",
        participants=["admin@corp.com"],
        first_message_at=t0,
        last_message_at=t0,
        message_count=1,
    )
    await store.create_thread(initial_thread)

    # Update with a new message turn
    t1 = datetime.now(UTC)
    updated = await store.update_thread_on_message(
        thread_id=thread_id,
        organization_id=org_id,
        message_time=t1,
        participants=["sre@corp.com", "admin@corp.com"],
        provider_thread_id="prov_th_assigned_later",
    )

    assert updated.message_count == 2
    assert updated.first_message_at == t0
    assert updated.last_message_at == t1
    assert "sre@corp.com" in updated.participants
    assert "admin@corp.com" in updated.participants
    assert updated.provider_thread_id == "prov_th_assigned_later"


@pytest.mark.asyncio
async def test_postgres_in_reply_to_and_references_lookup(db_pool: asyncpg.Pool) -> None:
    """Verify message parent lookup via In-Reply-To and References on PostgreSQL."""
    store = PostgresThreadStore(db_pool)

    org_id, mbx_id = uuid.uuid4(), uuid.uuid4()
    await ensure_test_mailbox(db_pool, org_id, mbx_id, "orders@corp.com")

    thread_id = uuid.uuid4()
    msg_id = uuid.uuid4()
    rfc822_id = f"parent_{uuid.uuid4().hex}@shop.com"

    # Seed thread
    await store.create_thread(
        EmailThread(
            id=thread_id,
            organization_id=org_id,
            mailbox_id=mbx_id,
            subject_normalized="Shipping Delay Notice",
            participants=["orders@corp.com"],
            message_count=1,
        )
    )

    # Seed email_message record linked to thread_id
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id,
                provider_message_id, rfc822_message_id, direction,
                received_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, 'inbound', now()
            );
            """,
            msg_id,
            org_id,
            mbx_id,
            thread_id,
            f"prov_{msg_id.hex}",
            rfc822_id,
        )

    # 1. Lookup by RFC 822 Message-ID
    found_thread_id = await store.find_by_rfc822_message_id(org_id, mbx_id, rfc822_id)
    assert found_thread_id == thread_id

    # 2. Lookup by References list
    ref_found = await store.find_by_references(
        org_id,
        mbx_id,
        ["non_existent@mail.com", rfc822_id],
    )
    assert ref_found == thread_id

    # 3. Cross-tenant isolation check
    other_org = uuid.uuid4()
    cross_tenant = await store.find_by_rfc822_message_id(other_org, mbx_id, rfc822_id)
    assert cross_tenant is None


@pytest.mark.asyncio
async def test_postgres_full_thread_associator_flow(db_pool: asyncpg.Pool) -> None:
    """Verify ThreadAssociator running end-to-end against live PostgreSQL."""
    store = PostgresThreadStore(db_pool)
    associator = ThreadAssociator(store=store, default_window_days=7)

    org_id, mbx_id = uuid.uuid4(), uuid.uuid4()
    await ensure_test_mailbox(db_pool, org_id, mbx_id, "billing@corp.com")

    # Step 1: Initial message arrives -> creates new thread (Tier 4)
    res1 = await associator.associate_message(
        organization_id=org_id,
        mailbox_id=mbx_id,
        subject_normalized="Invoice #1044 Payment",
        sender=EmailAddress("vendor@supplier.com"),
        recipients=[EmailAddress("billing@corp.com")],
        provider_thread_id="google_th_invoice_1044",
    )
    assert res1.is_new is True
    assert res1.reason == ThreadAssociationReason.NEW_THREAD
    thread_1_id = res1.thread_id

    # Step 2: Follow-up message arrives matching provider thread id (Tier 1)
    res2 = await associator.associate_message(
        organization_id=org_id,
        mailbox_id=mbx_id,
        subject_normalized="Re: Invoice #1044 Payment",
        sender=EmailAddress("billing@corp.com"),
        recipients=[EmailAddress("vendor@supplier.com")],
        provider_thread_id="google_th_invoice_1044",
    )
    assert res2.is_new is False
    assert res2.reason == ThreadAssociationReason.PROVIDER_THREAD_ID
    assert res2.thread_id == thread_1_id
    assert res2.thread.message_count == 2
