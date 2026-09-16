"""Integration tests for database schema, constraints, indexes, and dimension checks.

Requirements:
- R5.1: PostgreSQL with pgvector authoritative storage.
- R5.2: All core entities created.
- R5.3: organization_id on every tenant-scoped table.
- R5.4: Uniqueness on (org, mailbox, provider_msg_id) and (org, mailbox, provider_thread_id).
- R5.5: Versioned, reversible migrations.
- R5.6: GIN index on tsvector columns.
- R5.7: HNSW index on embedding_record.embedding.
- R5.10: Startup dimension assertion.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.db.migrator import (
    apply_migrations,
    rollback_migrations,
    verify_database_vector_dimension,
)

CORE_TABLES = [
    "organization",
    "mailbox",
    "mailbox_checkpoint",
    "email_thread",
    "email_message",
    "attachment",
    "classification_result",
    "processing_job",
    "thread_state",
    "generated_draft",
    "knowledge_document",
    "knowledge_chunk",
    "embedding_record",
    "feedback",
    "processing_event",
]

BUSINESS_TABLES = [
    "customer",
    "product",
    "order",
    "order_item",
    "ticket",
]


@pytest.fixture
async def db_pool() -> AsyncIterator[asyncpg.Pool[Any]]:
    """Asyncpg database connection pool."""
    settings = AppSettings().database
    # Ensure migrations applied
    await apply_migrations(dsn=settings.asyncpg_dsn)
    pool = await create_pool_from_settings(settings)
    try:
        yield pool
    finally:
        await pool.close()


async def test_extensions_installed(db_pool: asyncpg.Pool[Any]) -> None:
    """Verify vector and pg_trgm extensions are active (R5.1)."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')"
        )
        ext_names = {r["extname"] for r in rows}
        assert "vector" in ext_names
        assert "pg_trgm" in ext_names


async def test_all_entities_exist(db_pool: asyncpg.Pool[Any]) -> None:
    """Verify all 15 core entities and 5 business entities exist (R5.2, R13.1)."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            """
        )
        tables = {r["table_name"] for r in rows}
        for t in CORE_TABLES:
            assert t in tables, f"Missing core table: {t}"
        for t in BUSINESS_TABLES:
            assert t in tables, f"Missing business table: {t}"


async def test_tenant_scoping_columns(db_pool: asyncpg.Pool[Any]) -> None:
    """Verify organization_id column exists on every tenant table (R5.3)."""
    tenant_tables = [
        "mailbox",
        "mailbox_checkpoint",
        "email_thread",
        "email_message",
        "attachment",
        "classification_result",
        "processing_job",
        "thread_state",
        "generated_draft",
        "knowledge_document",
        "knowledge_chunk",
        "embedding_record",
        "feedback",
        "processing_event",
        "customer",
        "product",
        "order",
        "order_item",
        "ticket",
    ]
    async with db_pool.acquire() as conn:
        for t in tenant_tables:
            col = await conn.fetchval(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = $1
                  AND column_name = 'organization_id'
                """,
                t,
            )
            assert col == "organization_id", f"Table {t} missing organization_id"


async def test_composite_uniqueness_constraints(db_pool: asyncpg.Pool[Any]) -> None:
    """Verify uniqueness on (org, mailbox, provider_msg_id) and thread_id (R5.4)."""
    org_id = uuid.uuid4()
    mailbox_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    msg_id1 = uuid.uuid4()
    msg_id2 = uuid.uuid4()

    async with db_pool.acquire() as conn:
        try:
            # Seed organization and mailbox
            await conn.execute(
                "INSERT INTO organization (id, name) VALUES ($1, $2)",
                org_id,
                "Tenant Test Org",
            )
            await conn.execute(
                """
                INSERT INTO mailbox (id, organization_id, provider, address)
                VALUES ($1, $2, 'gmail', 'test@example.com')
                """,
                mailbox_id,
                org_id,
            )
            # 1. Thread uniqueness
            await conn.execute(
                """
                INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id)
                VALUES ($1, $2, $3, 'th-100')
                """,
                thread_id,
                org_id,
                mailbox_id,
            )
            # Duplicate thread insert must raise UniqueViolationError
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    """
                    INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id)
                    VALUES ($1, $2, $3, 'th-100')
                    """,
                    uuid.uuid4(),
                    org_id,
                    mailbox_id,
                )

            # 2. Message uniqueness
            now = datetime.now(UTC)
            await conn.execute(
                """
                INSERT INTO email_message (
                    id, organization_id, mailbox_id, thread_id,
                    provider_message_id, direction, received_at
                ) VALUES ($1, $2, $3, $4, 'msg-100', 'inbound', $5)
                """,
                msg_id1,
                org_id,
                mailbox_id,
                thread_id,
                now,
            )
            # Duplicate message insert must raise UniqueViolationError
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    """
                    INSERT INTO email_message (
                        id, organization_id, mailbox_id, thread_id,
                        provider_message_id, direction, received_at
                    ) VALUES ($1, $2, $3, $4, 'msg-100', 'inbound', $5)
                    """,
                    msg_id2,
                    org_id,
                    mailbox_id,
                    thread_id,
                    now,
                )
        finally:
            # Clean up test organization (cascades to mailboxes, threads, messages)
            await conn.execute("DELETE FROM organization WHERE id = $1", org_id)


async def test_gin_and_hnsw_indexes_exist(db_pool: asyncpg.Pool[Any]) -> None:
    """Verify GIN search indexes (R5.6) and HNSW vector index (R5.7) exist."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
            """
        )
        index_defs = {r["indexname"]: r["indexdef"] for r in rows}

        # GIN indexes (R5.6)
        assert any("gin" in d.lower() and "search_tsv" in d.lower() for d in index_defs.values())
        assert any("gin" in d.lower() and "content_tsv" in d.lower() for d in index_defs.values())

        # HNSW index (R5.7)
        assert any(
            "hnsw" in d.lower() and "vector_cosine_ops" in d.lower() for d in index_defs.values()
        )


async def test_vector_dimension_verification(db_pool: asyncpg.Pool[Any]) -> None:
    """Verify dimension check passes for 1536 and fails fast for mismatched dimension (R5.10)."""
    async with db_pool.acquire() as conn:
        # Correct dimension passes
        await verify_database_vector_dimension(conn_or_dsn=conn, configured_dimension=1536)

        # Mismatched dimension fails fast (R5.10)
        with pytest.raises(ValueError, match="does not match database column width"):
            await verify_database_vector_dimension(conn_or_dsn=conn, configured_dimension=768)


async def test_migration_reversibility() -> None:
    """Verify migration rollback drops tables and reapplying restores them (R5.5)."""
    settings = AppSettings().database

    # Roll back
    rolled_back = await rollback_migrations(dsn=settings.asyncpg_dsn, steps=1)
    assert "0001" in rolled_back

    # Verify tables dropped
    conn = await asyncpg.connect(settings.asyncpg_dsn)
    try:
        tables = await conn.fetch(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name != 'schema_migrations'
            """
        )
        assert len(tables) == 0
    finally:
        await conn.close()

    # Re-apply
    applied = await apply_migrations(dsn=settings.asyncpg_dsn)
    assert "0001" in applied
