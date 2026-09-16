"""Integration tests for database seed loader against live PostgreSQL and MinIO.

Requirements: R5.9, R10.11, R13.1.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from email import message_from_bytes
from typing import Any

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.core.storage import MinioObjectStorageClient
from packages.db.connection import create_pool_from_settings
from packages.db.fixtures.knowledge import (
    BETA_ORG_ID,
    DEMO_ORG_ID,
    GAMMA_ORG_ID,
    KNOWLEDGE_DOCS,
)
from packages.db.seed import deterministic_embed, seed_database
from tests.stubs import StubEmbedder


@pytest.fixture
async def db_pool() -> AsyncIterator[asyncpg.Pool[Any]]:
    """Provide a live connection pool to PostgreSQL container."""
    settings = AppSettings()
    pool = await create_pool_from_settings(settings.database)
    yield pool
    await pool.close()


@pytest.fixture
def storage_client() -> MinioObjectStorageClient:
    """Provide a live MinIO object storage client."""
    settings = AppSettings()
    return MinioObjectStorageClient(settings.object_storage)


async def test_seed_database_live_lifecycle(
    db_pool: asyncpg.Pool[Any],
    storage_client: MinioObjectStorageClient,
) -> None:
    """Validate full database seeding execution, entities count, and storage sync (R5.9)."""
    # 1. Execute seed with clean=True to ensure baseline
    summary = await seed_database(pool=db_pool, storage=storage_client, clean=True)

    expected_docs = len(KNOWLEDGE_DOCS)
    expected_chunks = sum(len(d.chunks) for d in KNOWLEDGE_DOCS)

    assert summary.tenants_count == 3
    assert summary.mailboxes_count == 4
    assert summary.customers_count == 4
    assert summary.products_count == 3
    assert summary.orders_count == 2
    assert summary.order_items_count == 2
    assert summary.tickets_count == 2
    assert summary.knowledge_docs_count == expected_docs
    assert summary.knowledge_chunks_count == expected_chunks
    assert summary.embeddings_count == expected_chunks
    assert summary.threads_count == 6
    assert summary.messages_count == 9
    assert summary.mime_objects_uploaded == 9

    # 2. Verify rows in PostgreSQL
    async with db_pool.acquire() as conn:
        org_count = await conn.fetchval(
            "SELECT count(*) FROM organization WHERE id = ANY($1::uuid[])",
            [DEMO_ORG_ID, BETA_ORG_ID, GAMMA_ORG_ID],
        )
        assert org_count == 3

        mbx_count = await conn.fetchval(
            "SELECT count(*) FROM mailbox WHERE organization_id = ANY($1::uuid[])",
            [DEMO_ORG_ID, BETA_ORG_ID, GAMMA_ORG_ID],
        )
        assert mbx_count == 4

        ckpt_count = await conn.fetchval(
            "SELECT count(*) FROM mailbox_checkpoint WHERE organization_id = ANY($1::uuid[])",
            [DEMO_ORG_ID, BETA_ORG_ID, GAMMA_ORG_ID],
        )
        assert ckpt_count == 4

        cust_count = await conn.fetchval(
            "SELECT count(*) FROM customer WHERE organization_id = $1",
            DEMO_ORG_ID,
        )
        assert cust_count == 4

        prod_count = await conn.fetchval(
            "SELECT count(*) FROM product WHERE organization_id = $1",
            DEMO_ORG_ID,
        )
        assert prod_count == 3

        order_count = await conn.fetchval(
            'SELECT count(*) FROM "order" WHERE organization_id = $1',
            DEMO_ORG_ID,
        )
        assert order_count == 2

        chunk_count = await conn.fetchval(
            "SELECT count(*) FROM knowledge_chunk WHERE organization_id = ANY($1::uuid[])",
            [DEMO_ORG_ID, BETA_ORG_ID, GAMMA_ORG_ID],
        )
        assert chunk_count == expected_chunks

        emb_count = await conn.fetchval(
            "SELECT count(*) FROM embedding_record WHERE organization_id = ANY($1::uuid[])",
            [DEMO_ORG_ID, BETA_ORG_ID, GAMMA_ORG_ID],
        )
        assert emb_count == expected_chunks

        thread_count = await conn.fetchval(
            "SELECT count(*) FROM email_thread WHERE organization_id = $1",
            DEMO_ORG_ID,
        )
        assert thread_count == 6

        msg_count = await conn.fetchval(
            "SELECT count(*) FROM email_message WHERE organization_id = $1",
            DEMO_ORG_ID,
        )
        assert msg_count == 9


async def test_seed_database_idempotency(
    db_pool: asyncpg.Pool[Any],
    storage_client: MinioObjectStorageClient,
) -> None:
    """Verify that re-running seed without clean produces no duplicates or errors (R5.9)."""
    expected_chunks = sum(len(d.chunks) for d in KNOWLEDGE_DOCS)

    # First pass
    await seed_database(pool=db_pool, storage=storage_client, clean=False)

    # Second pass
    summary = await seed_database(pool=db_pool, storage=storage_client, clean=False)
    assert summary.tenants_count == 3
    assert summary.messages_count == 9
    assert summary.knowledge_chunks_count == expected_chunks

    # Verify counts remain unchanged
    async with db_pool.acquire() as conn:
        msg_count = await conn.fetchval(
            "SELECT count(*) FROM email_message WHERE organization_id = $1",
            DEMO_ORG_ID,
        )
        assert msg_count == 9

        chunk_count = await conn.fetchval(
            "SELECT count(*) FROM knowledge_chunk WHERE organization_id = ANY($1::uuid[])",
            [DEMO_ORG_ID, BETA_ORG_ID, GAMMA_ORG_ID],
        )
        assert chunk_count == expected_chunks


async def test_seed_database_with_stub_embedder(
    db_pool: asyncpg.Pool[Any],
    storage_client: MinioObjectStorageClient,
) -> None:
    """Verify seeding accepts custom embedder test double (R24.5)."""
    expected_chunks = sum(len(d.chunks) for d in KNOWLEDGE_DOCS)
    stub = StubEmbedder(dimension=1536)
    summary = await seed_database(
        pool=db_pool,
        storage=storage_client,
        embedder=stub,
        clean=False,
    )
    assert summary.embeddings_count == expected_chunks
    assert stub.call_count == expected_chunks


async def test_seed_full_text_search_gin_indexes(db_pool: asyncpg.Pool[Any]) -> None:
    """Verify PostgreSQL GIN tsvector search operates over seeded records (R5.6)."""
    async with db_pool.acquire() as conn:
        # 1. Search knowledge chunks
        refund_chunks = await conn.fetch(
            """
            SELECT section, content
            FROM knowledge_chunk
            WHERE content_tsv @@ plainto_tsquery('english', 'refund')
              AND organization_id = $1
            """,
            DEMO_ORG_ID,
        )
        assert len(refund_chunks) >= 1
        assert "refund" in refund_chunks[0]["content"].lower()

        # 2. Search email messages
        crash_emails = await conn.fetch(
            """
            SELECT subject, body_text
            FROM email_message
            WHERE search_tsv @@ plainto_tsquery('english', 'MemoryAllocationError')
              AND organization_id = $1
            """,
            DEMO_ORG_ID,
        )
        assert len(crash_emails) >= 1
        assert "crashing" in crash_emails[0]["subject"].lower()


async def test_seed_pgvector_hnsw_similarity(db_pool: asyncpg.Pool[Any]) -> None:
    """Verify pgvector cosine similarity queries operate over seeded embeddings (R5.7, R10.11)."""
    target_text = (
        "Acme Corporation offers a 30-day money-back guarantee on all software "
        "subscriptions and hardware products. Customers requesting a refund within "
        "30 days of purchase will receive a full credit to their original payment method. "
        "To initiate a return or refund, submit ticket with invoice reference."
    )
    query_vec = deterministic_embed(target_text, dim=1536)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.content, e.embedding <=> $1::vector AS distance
            FROM embedding_record e
            JOIN knowledge_chunk c ON e.chunk_id = c.id
            WHERE e.organization_id = $2
            ORDER BY distance ASC
            LIMIT 1
            """,
            query_vec,
            DEMO_ORG_ID,
        )
        assert len(rows) == 1
        top_match = rows[0]
        # Exact match should have distance near zero
        assert top_match["distance"] < 1e-4
        assert "30-day money-back guarantee" in top_match["content"]


async def test_seed_minio_raw_mime_retrieval(
    db_pool: asyncpg.Pool[Any],
    storage_client: MinioObjectStorageClient,
) -> None:
    """Verify raw MIME files uploaded during seeding are retrievable and match RFC 822 (R5.8)."""
    async with db_pool.acquire() as conn:
        messages = await conn.fetch(
            """
            SELECT id, subject, sender_email, raw_object_key
            FROM email_message
            WHERE organization_id = $1
            """,
            DEMO_ORG_ID,
        )
        assert len(messages) >= 6

        for msg in messages:
            raw_key = msg["raw_object_key"]
            assert raw_key is not None

            # Verify object exists in MinIO
            exists = await storage_client.object_exists("raw-mime", raw_key)
            assert exists is True

            # Download and parse MIME
            raw_bytes = await storage_client.get_bytes("raw-mime", raw_key)
            parsed = message_from_bytes(raw_bytes)
            assert parsed["Subject"] == msg["subject"]
            assert msg["sender_email"] in parsed["From"]
