"""Integration tests for PostgresMessageStore with live PostgreSQL container.

Requirements:
- R4.8: Deduplication via ON CONFLICT (organization_id, mailbox_id, provider_message_id) DO NOTHING.
- R5.4: Uniqueness enforcement per mailbox and organization.
- R5.6: Write-time search_tsv generation with weights 'A' (subject) and 'B' (clean body).
- R4.7, R5.8: Attachment metadata persistence linked to email_message.
- GEMINI.md §8: Multi-tenant verification across >= 3 tenants with overlapping content.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.message import (
    AttachmentRecord,
    PostgresMessageStore,
)
from packages.domain.entities import (
    AttachmentRef,
    EmailAddress,
    NormalizedMessage,
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


async def ensure_test_thread(
    pool: asyncpg.Pool,
    org_id: uuid.UUID,
    mbx_id: uuid.UUID,
    thread_id: uuid.UUID,
    subject: str = "Quarterly Business Review",
) -> None:
    await ensure_test_mailbox(pool, org_id, mbx_id)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO email_thread (
                id, organization_id, mailbox_id, provider_thread_id, subject_normalized,
                first_message_at, last_message_at, message_count, participants, status
            ) VALUES (
                $1, $2, $3, $4, $5, now(), now(), 1, ARRAY['sender@corp.com'], 'open'
            ) ON CONFLICT (id) DO NOTHING;
            """,
            thread_id,
            org_id,
            mbx_id,
            str(thread_id),
            subject,
        )


def make_normalized_msg(
    org_id: uuid.UUID,
    mbx_id: uuid.UUID,
    thd_id: uuid.UUID,
    provider_msg_id: str,
    subject: str,
    body: str,
    received_at: datetime | None = None,
    attachments: list[AttachmentRef] | None = None,
) -> NormalizedMessage:
    msg_id = uuid.uuid4()
    return NormalizedMessage(
        message_id=msg_id,
        thread_id=thd_id,
        mailbox_id=mbx_id,
        organization_id=org_id,
        provider="gmail",
        provider_message_id=provider_msg_id,
        sender=EmailAddress(name="Alice Smith", email="alice@corp.com"),
        received_at=received_at or datetime.now(UTC),
        rfc822_message_id=f"<{msg_id}@corp.com>",
        in_reply_to=None,
        references_ids=[],
        recipients=[EmailAddress(name="Bob Jones", email="bob@corp.com")],
        cc=[],
        subject=subject,
        subject_normalized=subject,
        body_text=body,
        body_text_clean=body,
        snippet=body[:60],
        raw_object_key=f"raw/{msg_id}.eml",
        html_object_key=f"html/{msg_id}.html",
        direction="inbound",
        attachments=attachments or [],
        normalization_failed=False,
        signature_stripped=True,
    )


@pytest.mark.asyncio
async def test_postgres_message_store_multi_tenant_isolation(db_pool: asyncpg.Pool) -> None:
    """Validate that >= 3 tenants can store identical provider message IDs in isolation."""
    store = PostgresMessageStore(db_pool)

    # Setup 3 distinct tenants (GEMINI.md §8 mandate)
    tenants = [
        (uuid.uuid4(), uuid.uuid4(), uuid.uuid4()),  # (org, mbx, thd)
        (uuid.uuid4(), uuid.uuid4(), uuid.uuid4()),
        (uuid.uuid4(), uuid.uuid4(), uuid.uuid4()),
    ]

    shared_provider_id = f"shared-prov-id-{uuid.uuid4().hex[:8]}"
    shared_subject = "Urgent Security Advisory Regarding CVE-2026-999"
    shared_body = "A critical vulnerability requires immediate server patching."

    messages: list[NormalizedMessage] = []
    for org_id, mbx_id, thd_id in tenants:
        await ensure_test_thread(db_pool, org_id, mbx_id, thd_id, subject=shared_subject)
        msg = make_normalized_msg(
            org_id=org_id,
            mbx_id=mbx_id,
            thd_id=thd_id,
            provider_msg_id=shared_provider_id,
            subject=shared_subject,
            body=shared_body,
        )
        messages.append(msg)
        res = await store.insert_message(msg)
        assert res.inserted is True
        assert res.is_duplicate is False

    # Verify each tenant can retrieve only their own message
    for i, (org_id, _mbx_id, _) in enumerate(tenants):
        retrieved = await store.get_message(org_id, messages[i].message_id)
        assert retrieved is not None
        assert retrieved.organization_id == org_id
        assert retrieved.provider_message_id == shared_provider_id

        # Verify cross-tenant isolation: other tenants cannot view this message
        for j, (other_org, _, _) in enumerate(tenants):
            if i != j:
                cross_view = await store.get_message(other_org, messages[i].message_id)
                assert cross_view is None


@pytest.mark.asyncio
async def test_postgres_message_store_deduplication_on_conflict(db_pool: asyncpg.Pool) -> None:
    """Validate ON CONFLICT DO NOTHING returns inserted=False, is_duplicate=True without errors."""
    store = PostgresMessageStore(db_pool)

    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    thd_id = uuid.uuid4()
    await ensure_test_thread(db_pool, org_id, mbx_id, thd_id)

    provider_msg_id = f"replay-test-{uuid.uuid4().hex[:8]}"
    msg1 = make_normalized_msg(
        org_id=org_id,
        mbx_id=mbx_id,
        thd_id=thd_id,
        provider_msg_id=provider_msg_id,
        subject="Invoice #8841",
        body="Attached please find your invoice for September.",
    )

    # First insert
    res1 = await store.insert_message(msg1)
    assert res1.inserted is True
    assert res1.is_duplicate is False
    assert res1.message_id == msg1.message_id

    # Second insert with identical org_id, mailbox_id, provider_message_id
    msg2 = make_normalized_msg(
        org_id=org_id,
        mbx_id=mbx_id,
        thd_id=thd_id,
        provider_msg_id=provider_msg_id,
        subject="Invoice #8841",
        body="Attached please find your invoice for September.",
    )
    res2 = await store.insert_message(msg2)
    assert res2.inserted is False
    assert res2.is_duplicate is True


@pytest.mark.asyncio
async def test_postgres_message_store_attachments_persistence(db_pool: asyncpg.Pool) -> None:
    """Validate atomic persistence of attachment metadata linked to email_message."""
    store = PostgresMessageStore(db_pool)

    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    thd_id = uuid.uuid4()
    await ensure_test_thread(db_pool, org_id, mbx_id, thd_id)

    att1 = AttachmentRef(
        filename="contract.pdf",
        mime_type="application/pdf",
        size_bytes=524288,
        object_key=f"attachments/{org_id}/contract.pdf",
        checksum="sha256:11112222",
    )
    att2 = AttachmentRef(
        filename="spreadsheet.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=1048576,
        object_key=f"attachments/{org_id}/spreadsheet.xlsx",
        checksum="sha256:33334444",
    )

    msg = make_normalized_msg(
        org_id=org_id,
        mbx_id=mbx_id,
        thd_id=thd_id,
        provider_msg_id=f"msg-with-atts-{uuid.uuid4().hex[:8]}",
        subject="Executed Agreement and Financials",
        body="Here are the signed documents.",
        attachments=[att1, att2],
    )

    res = await store.insert_message(msg)
    assert res.inserted is True

    # Check attachments records in attachment table
    records = await store.get_attachments(org_id, msg.message_id)
    assert len(records) == 2
    assert all(isinstance(r, AttachmentRecord) for r in records)

    filenames = {r.filename for r in records}
    assert filenames == {"contract.pdf", "spreadsheet.xlsx"}

    # Re-retrieve message through store
    retrieved = await store.get_message(org_id, msg.message_id)
    assert retrieved is not None
    assert len(retrieved.attachments) == 2


@pytest.mark.asyncio
async def test_postgres_message_store_search_tsv_gin(db_pool: asyncpg.Pool) -> None:
    """Validate write-time search_tsv generation and GIN index full-text search (R5.6)."""
    store = PostgresMessageStore(db_pool)

    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    thd_id = uuid.uuid4()
    await ensure_test_thread(db_pool, org_id, mbx_id, thd_id)

    msg = make_normalized_msg(
        org_id=org_id,
        mbx_id=mbx_id,
        thd_id=thd_id,
        provider_msg_id=f"fts-msg-{uuid.uuid4().hex[:8]}",
        subject="Database Cluster Failover Completed",
        body="The PostgreSQL primary node switched to replica-02 without data loss.",
    )
    await store.insert_message(msg)

    # Search for subject keyword ('A' weight)
    results_subj = await store.search_messages_by_text(org_id, "Failover")
    assert len(results_subj) >= 1
    assert any(m.message_id == msg.message_id for m in results_subj)

    # Search for body keyword ('B' weight)
    results_body = await store.search_messages_by_text(org_id, "replica")
    assert len(results_body) >= 1
    assert any(m.message_id == msg.message_id for m in results_body)

    # Search in different tenant should return empty (tenant isolation)
    other_org = uuid.uuid4()
    results_other = await store.search_messages_by_text(other_org, "Failover")
    assert len(results_other) == 0


@pytest.mark.asyncio
async def test_postgres_message_store_thread_ordered_messages(db_pool: asyncpg.Pool) -> None:
    """Validate that messages in a thread are returned sorted by received_at ASC."""
    store = PostgresMessageStore(db_pool)

    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    thd_id = uuid.uuid4()
    await ensure_test_thread(db_pool, org_id, mbx_id, thd_id)

    base_time = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)

    msg1 = make_normalized_msg(
        org_id=org_id,
        mbx_id=mbx_id,
        thd_id=thd_id,
        provider_msg_id=f"thd-msg-1-{uuid.uuid4().hex[:8]}",
        subject="Budget Review",
        body="Initial budget proposal.",
        received_at=base_time,
    )
    msg2 = make_normalized_msg(
        org_id=org_id,
        mbx_id=mbx_id,
        thd_id=thd_id,
        provider_msg_id=f"thd-msg-2-{uuid.uuid4().hex[:8]}",
        subject="Re: Budget Review",
        body="Budget approved with minor changes.",
        received_at=base_time + timedelta(hours=1),
    )

    await store.insert_message(msg1)
    await store.insert_message(msg2)

    thread_msgs = await store.get_messages_by_thread(org_id, thd_id)
    assert len(thread_msgs) == 2
    assert thread_msgs[0].message_id == msg1.message_id
    assert thread_msgs[1].message_id == msg2.message_id
    assert thread_msgs[0].received_at < thread_msgs[1].received_at
