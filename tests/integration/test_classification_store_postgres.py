"""PostgreSQL integration tests for PostgresClassificationStore (R5.2, R6.7, design.md §6.1).

Verifies:
1. End-to-end insertion and retrieval of classification results in live PostgreSQL.
2. Tenant isolation ensuring cross-tenant queries return None (R5.3).
3. Retrieval of latest classification for a given message.
4. Database CHECK constraint compliance on decided_by IN ('rule', 'ml', 'llm', 'default').
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.classification import PostgresClassificationStore
from packages.db.connection import create_pool_from_settings
from packages.domain.entities import Classification


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    """Provide an asyncpg connection pool connected to live postgres."""
    settings = AppSettings()
    pool = await create_pool_from_settings(settings.database)
    try:
        yield pool
    finally:
        await pool.close()


async def ensure_test_email(
    pool: asyncpg.Pool,
    org_id: uuid.UUID,
    mbx_id: uuid.UUID,
    msg_id: uuid.UUID,
    thread_id: uuid.UUID | None = None,
) -> None:
    """Seed prerequisite organization, mailbox, thread, and message records."""
    t_id = thread_id or uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING;",
            org_id,
            f"Test Org {org_id.hex[:6]}",
        )
        await conn.execute(
            """
            INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)
            VALUES ($1, $2, 'gmail', $3, 'Test Mailbox', 'active')
            ON CONFLICT (id) DO NOTHING;
            """,
            mbx_id,
            org_id,
            f"box-{mbx_id.hex[:6]}@example.com",
        )
        await conn.execute(
            """
            INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (id) DO NOTHING;
            """,
            t_id,
            org_id,
            mbx_id,
            f"th-{t_id.hex[:6]}",
        )
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                direction, sender_email, sender_name, recipients, subject, body_text, received_at
            ) VALUES (
                $1, $2, $3, $4, $5,
                'inbound', 'test@client.com', 'Test Sender',
                '[]', 'Test Subject', 'Body text', now()
            )
            ON CONFLICT (id) DO NOTHING;
            """,
            msg_id,
            org_id,
            mbx_id,
            t_id,
            f"pmsg-{msg_id.hex[:6]}",
        )


@pytest.mark.asyncio
async def test_save_and_get_classification(db_pool: asyncpg.Pool) -> None:
    """Verify persisting and retrieving a classification result in PostgreSQL."""
    store = PostgresClassificationStore(db_pool)
    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    msg_id = uuid.uuid4()
    await ensure_test_email(db_pool, org_id, mbx_id, msg_id)

    domain_cls = Classification(
        category="billing",
        intent="invoice_query",
        priority="high",
        reply_required=True,
        workflow_hint="ai",
        retrieval_required=True,
        confidence=0.965,
        decided_by="ml",
        latency_ms=24,
        model="tfidf-logistic-v1",
        raw={"probabilities": {"billing": 0.965, "support": 0.035}},
    )

    saved_row = await store.save_classification(
        organization_id=org_id,
        message_id=msg_id,
        classification=domain_cls,
    )

    assert saved_row.organization_id == org_id
    assert saved_row.message_id == msg_id
    assert saved_row.category == "billing"
    assert saved_row.intent == "invoice_query"
    assert saved_row.priority == "high"
    assert saved_row.reply_required is True
    assert saved_row.retrieval_required is True
    assert round(saved_row.confidence, 3) == 0.965
    assert saved_row.decided_by == "ml"
    assert saved_row.model_name == "tfidf-logistic-v1"
    assert saved_row.latency_ms == 24
    assert saved_row.raw["probabilities"]["billing"] == 0.965
    assert saved_row.raw["workflow_hint"] == "ai"

    # Fetch by ID
    fetched_row = await store.get_classification(org_id, saved_row.id)
    assert fetched_row is not None
    assert fetched_row.id == saved_row.id
    assert fetched_row.category == "billing"

    # Convert back to domain entity
    restored_domain = fetched_row.to_domain_classification()
    assert restored_domain.category == "billing"
    assert restored_domain.workflow_hint == "ai"
    assert restored_domain.decided_by == "ml"


@pytest.mark.asyncio
async def test_latest_classification_and_list_by_message(db_pool: asyncpg.Pool) -> None:
    """Verify latest classification retrieval and chronological listing."""
    store = PostgresClassificationStore(db_pool)
    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    msg_id = uuid.uuid4()
    await ensure_test_email(db_pool, org_id, mbx_id, msg_id)

    # First attempt: Stage 1 rule abstains or low conf
    cls1 = Classification(
        category="support",
        confidence=0.70,
        decided_by="rule",
    )
    r1 = await store.save_classification(org_id, msg_id, cls1)

    # Second attempt: Stage 2 ML accepts
    cls2 = Classification(
        category="support",
        confidence=0.92,
        decided_by="ml",
    )
    r2 = await store.save_classification(org_id, msg_id, cls2)

    # Query latest: must return r2
    latest = await store.get_latest_classification_by_message(org_id, msg_id)
    assert latest is not None
    assert latest.id == r2.id
    assert latest.decided_by == "ml"
    assert round(latest.confidence, 2) == 0.92

    # Query all: returns both in chronological order
    all_rows = await store.list_classifications_by_message(org_id, msg_id)
    assert len(all_rows) >= 2
    row_ids = [r.id for r in all_rows]
    assert r1.id in row_ids
    assert r2.id in row_ids


@pytest.mark.asyncio
async def test_tenant_scoping_isolation(db_pool: asyncpg.Pool) -> None:
    """Verify classification rows cannot be fetched by a different organization (R5.3)."""
    store = PostgresClassificationStore(db_pool)
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    mbx_id = uuid.uuid4()
    msg_id = uuid.uuid4()
    await ensure_test_email(db_pool, org_a, mbx_id, msg_id)

    cls = Classification(category="support", decided_by="rule")
    saved = await store.save_classification(org_a, msg_id, cls)

    # Fetching with Org B returns None
    assert await store.get_classification(org_b, saved.id) is None
    assert await store.get_latest_classification_by_message(org_b, msg_id) is None
    assert await store.list_classifications_by_message(org_b, msg_id) == []


@pytest.mark.asyncio
async def test_decided_by_check_constraint_variants(db_pool: asyncpg.Pool) -> None:
    """Verify all 4 enum values of decided_by pass DB check constraint."""
    store = PostgresClassificationStore(db_pool)
    org_id = uuid.uuid4()
    mbx_id = uuid.uuid4()
    msg_id = uuid.uuid4()
    await ensure_test_email(db_pool, org_id, mbx_id, msg_id)

    for variant in ("rule", "ml", "llm", "default"):
        c = Classification(category="general_inquiry", decided_by=variant)
        row = await store.save_classification(org_id, msg_id, c)
        assert row.decided_by == variant

    # Invalid value triggers check constraint failure
    with pytest.raises(asyncpg.CheckViolationError):
        invalid_cls = Classification(category="general_inquiry", decided_by="invalid_source")
        await store.save_classification(org_id, msg_id, invalid_cls)
