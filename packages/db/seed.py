"""Database fixture seed loader and CLI (R5.9, R10.11, R13.1).

Seeds realistic fixture data across 3 organizations (Acme, Beta, Gamma):
- Tenant organizations with settings
- Configured mailboxes and sync checkpoints
- Relational CRM/ERP business entities (customers, products, orders, items, tickets)
- Knowledge documents, chunks, GIN tsvectors, and 1536-dim HNSW embeddings
- Multi-archetype email threads, messages, search tsvectors, and raw MIME storage

Idempotent: safe to run multiple times using ON CONFLICT upserting.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formatdate
from typing import Any
from uuid import UUID, uuid5

import asyncpg

from packages.core.settings import AppSettings
from packages.core.storage import MinioObjectStorageClient, ObjectKeyBuilder, StorageProtocol
from packages.db.connection import create_pool_from_settings
from packages.db.fixtures.business import (
    BUSINESS_CUSTOMERS,
    BUSINESS_ORDER_ITEMS,
    BUSINESS_ORDERS,
    BUSINESS_PRODUCTS,
    BUSINESS_TICKETS,
)
from packages.db.fixtures.emails import FIXTURE_EMAILS
from packages.db.fixtures.knowledge import (
    BETA_ORG_ID,
    DEMO_ORG_ID,
    GAMMA_ORG_ID,
    KNOWLEDGE_DOCS,
    TENANT_ORGS,
)

logger = logging.getLogger(__name__)

# Deterministic namespace UUID for stable seed generation
SEED_NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

# Stable mailbox identifiers
ACME_SUPPORT_MBX_ID = UUID("00000000-0000-0000-0001-000000000001")
ACME_BILLING_MBX_ID = UUID("00000000-0000-0000-0001-000000000002")
BETA_SUPPORT_MBX_ID = UUID("00000000-0000-0000-0002-000000000001")
GAMMA_DISPATCH_MBX_ID = UUID("00000000-0000-0000-0003-000000000001")

SEED_MAILBOXES: list[dict[str, Any]] = [
    {
        "id": ACME_SUPPORT_MBX_ID,
        "organization_id": DEMO_ORG_ID,
        "provider": "gmail",
        "address": "support@acme.com",
        "display_name": "Acme Customer Support",
        "status": "active",
    },
    {
        "id": ACME_BILLING_MBX_ID,
        "organization_id": DEMO_ORG_ID,
        "provider": "graph",
        "address": "billing@acme.com",
        "display_name": "Acme Billing & Accounts",
        "status": "active",
    },
    {
        "id": BETA_SUPPORT_MBX_ID,
        "organization_id": BETA_ORG_ID,
        "provider": "imap",
        "address": "support@beta.com",
        "display_name": "Beta Support",
        "status": "active",
    },
    {
        "id": GAMMA_DISPATCH_MBX_ID,
        "organization_id": GAMMA_ORG_ID,
        "provider": "imap",
        "address": "dispatch@gamma.com",
        "display_name": "Gamma Logistics Operations",
        "status": "active",
    },
]


@dataclass(frozen=True)
class SeedSummary:
    """Summary of database seed operation results."""

    tenants_count: int
    mailboxes_count: int
    customers_count: int
    products_count: int
    orders_count: int
    order_items_count: int
    tickets_count: int
    knowledge_docs_count: int
    knowledge_chunks_count: int
    embeddings_count: int
    threads_count: int
    messages_count: int
    mime_objects_uploaded: int


def deterministic_embed(text: str, dim: int = 1536) -> list[float]:
    """Fallback deterministic L2-normalized vector generator for seed embeddings."""
    hasher = hashlib.sha256(text.encode("utf-8"))
    seed_bytes = hasher.digest()

    raw_values: list[float] = []
    for i in range(dim):
        b = seed_bytes[(i + (i // len(seed_bytes))) % len(seed_bytes)]
        val = (b / 127.5) - 1.0
        val += 0.1 * math.sin(i * 0.1)
        raw_values.append(val)

    norm = math.sqrt(sum(x * x for x in raw_values))
    if norm == 0:
        return [0.0] * dim
    return [x / norm for x in raw_values]


async def seed_database(
    pool: asyncpg.Pool[Any],
    storage: StorageProtocol | None = None,
    embedder: Any | None = None,
    clean: bool = False,
) -> SeedSummary:
    """Seed the database with organizations, business records, knowledge, and emails.

    Args:
        pool: Active asyncpg connection pool.
        storage: Optional object storage client for uploading raw MIME payloads.
        embedder: Optional embedding provider test double or callable.
        clean: If True, purges existing seed tenant data before re-inserting.

    Returns:
        SeedSummary with counts of all seeded entities.
    """
    async with pool.acquire() as conn, conn.transaction():
        if clean:
            logger.info("Cleaning existing seed tenant data...")
            target_orgs = [DEMO_ORG_ID, BETA_ORG_ID, GAMMA_ORG_ID]
            await conn.execute(
                "DELETE FROM organization WHERE id = ANY($1::uuid[])",
                target_orgs,
            )

        # 1. Seed Organizations
        logger.info("Seeding %d tenant organizations...", len(TENANT_ORGS))
        for org in TENANT_ORGS:
            await conn.execute(
                """
                INSERT INTO organization (id, name, settings)
                VALUES ($1, $2, $3::jsonb)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    settings = EXCLUDED.settings
                """,
                org["id"],
                org["name"],
                json.dumps(org["settings"]),
            )

        # 2. Seed Mailboxes & Checkpoints
        logger.info("Seeding %d mailboxes...", len(SEED_MAILBOXES))
        for mbx in SEED_MAILBOXES:
            await conn.execute(
                """
                INSERT INTO mailbox (
                    id, organization_id, provider, address, display_name, status
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (organization_id, address) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    status = EXCLUDED.status
                """,
                mbx["id"],
                mbx["organization_id"],
                mbx["provider"],
                mbx["address"],
                mbx["display_name"],
                mbx["status"],
            )
            await conn.execute(
                """
                INSERT INTO mailbox_checkpoint (
                    mailbox_id, organization_id, history_id, sync_state, last_sync_at
                ) VALUES ($1, $2, 'seed-checkpoint-001', 'idle', now())
                ON CONFLICT (mailbox_id) DO NOTHING
                """,
                mbx["id"],
                mbx["organization_id"],
            )

        # 3. Seed Business CRM & ERP Records
        logger.info("Seeding business records (customers, products, orders, tickets)...")
        for cust in BUSINESS_CUSTOMERS:
            await conn.execute(
                """
                INSERT INTO customer (id, organization_id, email, name, account_status, tier)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    email = EXCLUDED.email,
                    account_status = EXCLUDED.account_status,
                    tier = EXCLUDED.tier
                """,
                cust["id"],
                cust["organization_id"],
                cust["email"],
                cust["name"],
                cust["account_status"],
                cust["tier"],
            )

        for prod in BUSINESS_PRODUCTS:
            await conn.execute(
                """
                INSERT INTO product (id, organization_id, sku, name, price, status)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (id) DO UPDATE SET
                    sku = EXCLUDED.sku,
                    name = EXCLUDED.name,
                    price = EXCLUDED.price,
                    status = EXCLUDED.status
                """,
                prod["id"],
                prod["organization_id"],
                prod["sku"],
                prod["name"],
                prod["price"],
                prod["status"],
            )

        for order in BUSINESS_ORDERS:
            await conn.execute(
                """
                INSERT INTO "order" (
                    id, organization_id, customer_id, order_number, status, total, placed_at
                ) VALUES ($1, $2, $3, $4, $5, $6, now() - INTERVAL '2 days')
                ON CONFLICT (id) DO UPDATE SET
                    order_number = EXCLUDED.order_number,
                    status = EXCLUDED.status,
                    total = EXCLUDED.total
                """,
                order["id"],
                order["organization_id"],
                order["customer_id"],
                order["order_number"],
                order["status"],
                order["total"],
            )

        for item in BUSINESS_ORDER_ITEMS:
            await conn.execute(
                """
                INSERT INTO order_item (
                    id, organization_id, order_id, product_id, quantity, unit_price
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (id) DO NOTHING
                """,
                item["id"],
                item["organization_id"],
                item["order_id"],
                item["product_id"],
                item["quantity"],
                item["unit_price"],
            )

        for ticket in BUSINESS_TICKETS:
            await conn.execute(
                """
                INSERT INTO ticket (
                    id, organization_id, customer_id, ticket_number, status,
                    priority, subject, opened_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, now() - INTERVAL '1 day')
                ON CONFLICT (id) DO UPDATE SET
                    ticket_number = EXCLUDED.ticket_number,
                    status = EXCLUDED.status,
                    priority = EXCLUDED.priority,
                    subject = EXCLUDED.subject
                """,
                ticket["id"],
                ticket["organization_id"],
                ticket["customer_id"],
                ticket["ticket_number"],
                ticket["status"],
                ticket["priority"],
                ticket["subject"],
            )

        # 4. Seed Knowledge Documents, Chunks, and Embeddings
        logger.info("Seeding %d knowledge documents with embeddings...", len(KNOWLEDGE_DOCS))
        chunk_count = 0
        embedding_count = 0

        for doc in KNOWLEDGE_DOCS:
            doc_id = uuid5(SEED_NAMESPACE, f"knowledge_doc:{doc.org_id}:{doc.title}")
            await conn.execute(
                """
                INSERT INTO knowledge_document (id, organization_id, title, category, status)
                VALUES ($1, $2, $3, $4, 'active')
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    category = EXCLUDED.category,
                    status = EXCLUDED.status
                """,
                doc_id,
                doc.org_id,
                doc.title,
                doc.source_type,
            )

            for chunk in doc.chunks:
                chunk_id = uuid5(SEED_NAMESPACE, f"chunk:{doc_id}:{chunk.chunk_index}")
                await conn.execute(
                    """
                    INSERT INTO knowledge_chunk (
                        id, document_id, organization_id, chunk_index, section, category,
                        content, version, content_tsv
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, 1, to_tsvector('english', $7)
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        content = EXCLUDED.content,
                        content_tsv = to_tsvector('english', EXCLUDED.content)
                    """,
                    chunk_id,
                    doc_id,
                    doc.org_id,
                    chunk.chunk_index,
                    chunk.title,
                    doc.source_type,
                    chunk.content,
                )
                chunk_count += 1

                # Compute and insert embedding
                if embedder is not None and hasattr(embedder, "embed_text"):
                    vec = embedder.embed_text(chunk.content)
                else:
                    vec = deterministic_embed(chunk.content, dim=1536)

                await conn.execute(
                    """
                    INSERT INTO embedding_record (
                        chunk_id, organization_id, model, dim, embedding
                    ) VALUES ($1, $2, 'text-embedding-3-small', 1536, $3)
                    ON CONFLICT (chunk_id) DO UPDATE SET
                        embedding = EXCLUDED.embedding
                    """,
                    chunk_id,
                    doc.org_id,
                    vec,
                )
                embedding_count += 1

        # 5. Seed Email Threads, Messages, and Object Storage
        logger.info("Seeding %d fixture emails and threads...", len(FIXTURE_EMAILS))
        if storage is not None:
            try:
                await storage.bootstrap_buckets()
            except Exception as b_err:
                logger.warning("Bucket bootstrap skipped or failed: %s", b_err)

        threads_count = 0
        messages_count = 0
        mime_uploaded = 0

        now_dt = datetime.now(UTC)

        for fixture in FIXTURE_EMAILS:
            # Route to appropriate mailbox
            if fixture.category == "billing":
                target_mbx_id = ACME_BILLING_MBX_ID
            else:
                target_mbx_id = ACME_SUPPORT_MBX_ID

            thread_id = uuid5(SEED_NAMESPACE, f"thread:{fixture.key}")
            participants = list(set([fixture.sender_email] + fixture.recipients))
            total_msgs = 1 + len(fixture.thread_history)

            await conn.execute(
                """
                INSERT INTO email_thread (
                    id, organization_id, mailbox_id, provider_thread_id,
                    subject_normalized, participants, first_message_at, last_message_at,
                    message_count, status
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, 'open'
                )
                ON CONFLICT (organization_id, mailbox_id, provider_thread_id) DO UPDATE SET
                    subject_normalized = EXCLUDED.subject_normalized,
                    participants = EXCLUDED.participants,
                    message_count = EXCLUDED.message_count
                """,
                thread_id,
                DEMO_ORG_ID,
                target_mbx_id,
                f"thread-{fixture.key}",
                fixture.subject.lower().strip(),
                participants,
                now_dt,
                now_dt,
                total_msgs,
            )
            threads_count += 1

            # Prior turns in thread history
            for turn_idx, turn in enumerate(fixture.thread_history):
                turn_msg_id = uuid5(SEED_NAMESPACE, f"msg:{fixture.key}:turn:{turn_idx}")
                provider_msg_id = f"msg-{fixture.key}-turn-{turn_idx}"
                raw_key = ObjectKeyBuilder.raw_mime(DEMO_ORG_ID, target_mbx_id, turn_msg_id)

                # Build turn MIME
                turn_msg = EmailMessage()
                turn_msg["Subject"] = fixture.subject
                turn_msg["From"] = f"{turn.sender_name} <{turn.sender_email}>"
                turn_msg["To"] = ", ".join(fixture.recipients)
                turn_msg["Date"] = formatdate(localtime=True)
                turn_msg["Message-ID"] = f"<{provider_msg_id}@fixture.test>"
                if turn.in_reply_to:
                    turn_msg["In-Reply-To"] = turn.in_reply_to
                if turn.references:
                    turn_msg["References"] = " ".join(turn.references)
                turn_msg.set_content(turn.body_text)
                turn_raw_bytes = turn_msg.as_bytes()

                if storage is not None:
                    try:
                        await storage.put_bytes(
                            bucket="raw-mime",
                            key=raw_key,
                            data=turn_raw_bytes,
                            content_type="message/rfc822",
                        )
                        mime_uploaded += 1
                    except Exception as s_err:
                        logger.warning("Failed uploading MIME payload for %s: %s", raw_key, s_err)

                await conn.execute(
                    """
                    INSERT INTO email_message (
                        id, organization_id, mailbox_id, thread_id,
                        provider_message_id, direction, sender_email, sender_name,
                        recipients, cc, subject, subject_normalized, body_text,
                        body_text_clean, raw_object_key, received_at, search_tsv
                    ) VALUES (
                        $1, $2, $3, $4, $5, 'inbound', $6, $7, $8::jsonb, '[]'::jsonb,
                        $9, $10, $11, $11, $12, $13,
                        to_tsvector('english', COALESCE($9, '') || ' ' || COALESCE($11, ''))
                    )
                    ON CONFLICT (organization_id, mailbox_id, provider_message_id) DO UPDATE SET
                        subject = EXCLUDED.subject,
                        body_text = EXCLUDED.body_text,
                        raw_object_key = EXCLUDED.raw_object_key,
                        search_tsv = EXCLUDED.search_tsv
                    """,
                    turn_msg_id,
                    DEMO_ORG_ID,
                    target_mbx_id,
                    thread_id,
                    provider_msg_id,
                    turn.sender_email,
                    turn.sender_name,
                    json.dumps(fixture.recipients),
                    fixture.subject,
                    fixture.subject.lower().strip(),
                    turn.body_text,
                    raw_key,
                    now_dt,
                )
                messages_count += 1

            # Primary / latest fixture message
            main_msg_id = uuid5(SEED_NAMESPACE, f"msg:{fixture.key}:primary")
            main_provider_id = f"msg-{fixture.key}-primary"
            main_raw_key = ObjectKeyBuilder.raw_mime(DEMO_ORG_ID, target_mbx_id, main_msg_id)
            main_raw_bytes = fixture.to_raw_mime(provider_message_id=main_provider_id)

            if storage is not None:
                try:
                    await storage.put_bytes(
                        bucket="raw-mime",
                        key=main_raw_key,
                        data=main_raw_bytes,
                        content_type="message/rfc822",
                    )
                    mime_uploaded += 1
                except Exception as s_err:
                    logger.warning("Failed uploading MIME payload for %s: %s", main_raw_key, s_err)

            await conn.execute(
                """
                INSERT INTO email_message (
                    id, organization_id, mailbox_id, thread_id,
                    provider_message_id, direction, sender_email, sender_name,
                    recipients, cc, subject, subject_normalized, body_text,
                    body_text_clean, raw_object_key, received_at, search_tsv
                ) VALUES (
                    $1, $2, $3, $4, $5, 'inbound', $6, $7, $8::jsonb, '[]'::jsonb,
                    $9, $10, $11, $11, $12, $13,
                    to_tsvector('english', COALESCE($9, '') || ' ' || COALESCE($11, ''))
                )
                ON CONFLICT (organization_id, mailbox_id, provider_message_id) DO UPDATE SET
                    subject = EXCLUDED.subject,
                    body_text = EXCLUDED.body_text,
                    raw_object_key = EXCLUDED.raw_object_key,
                    search_tsv = EXCLUDED.search_tsv
                """,
                main_msg_id,
                DEMO_ORG_ID,
                target_mbx_id,
                thread_id,
                main_provider_id,
                fixture.sender_email,
                fixture.sender_name,
                json.dumps(fixture.recipients),
                fixture.subject,
                fixture.subject.lower().strip(),
                fixture.body_text,
                main_raw_key,
                now_dt,
            )
            messages_count += 1

    summary = SeedSummary(
        tenants_count=len(TENANT_ORGS),
        mailboxes_count=len(SEED_MAILBOXES),
        customers_count=len(BUSINESS_CUSTOMERS),
        products_count=len(BUSINESS_PRODUCTS),
        orders_count=len(BUSINESS_ORDERS),
        order_items_count=len(BUSINESS_ORDER_ITEMS),
        tickets_count=len(BUSINESS_TICKETS),
        knowledge_docs_count=len(KNOWLEDGE_DOCS),
        knowledge_chunks_count=chunk_count,
        embeddings_count=embedding_count,
        threads_count=threads_count,
        messages_count=messages_count,
        mime_objects_uploaded=mime_uploaded,
    )
    logger.info("Database seeding completed successfully: %s", summary)
    return summary


async def async_main() -> None:
    """CLI execution entrypoint."""
    parser = argparse.ArgumentParser(
        description="Seed database with reference fixtures and archetypes."
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Purge existing seed tenant entities prior to re-seeding.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    settings = AppSettings()
    pool = await create_pool_from_settings(settings.database)

    storage: MinioObjectStorageClient | None = None
    try:
        storage = MinioObjectStorageClient(settings.object_storage)
    except Exception as s_err:
        logger.warning(
            "Object storage client initialization failed; continuing without storage: %s",
            s_err,
        )

    try:
        summary = await seed_database(pool=pool, storage=storage, clean=args.clean)
        print("\n=== Database Fixture Seed Summary ===")
        print(f"Tenants seeded:           {summary.tenants_count}")
        print(f"Mailboxes seeded:         {summary.mailboxes_count}")
        print(f"Customers seeded:         {summary.customers_count}")
        print(f"Products seeded:          {summary.products_count}")
        print(f"Orders seeded:            {summary.orders_count}")
        print(f"Order items seeded:       {summary.order_items_count}")
        print(f"Tickets seeded:           {summary.tickets_count}")
        print(f"Knowledge docs seeded:    {summary.knowledge_docs_count}")
        print(f"Knowledge chunks seeded:  {summary.knowledge_chunks_count}")
        print(f"Embeddings seeded:        {summary.embeddings_count}")
        print(f"Email threads seeded:     {summary.threads_count}")
        print(f"Email messages seeded:    {summary.messages_count}")
        print(f"MIME objects in MinIO:    {summary.mime_objects_uploaded}")
        print("====================================\n")
    finally:
        await pool.close()


def main() -> None:
    """Synchronous runner for CLI entrypoint."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
