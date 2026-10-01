"""Evaluation mailbox for the live benchmark's feeder (CLAUDE.md §4, R1.3).

The feeder hands each case email to the pipeline exactly where the mail connector does, after a
fetch, and every row the pipeline writes points at a mailbox. That mailbox is an ``imap`` one.
The provider name is confined to ``packages/adapters/`` like every other, so the feeder calls
``create_eval_mailbox`` and never names a provider itself.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import asyncpg

EVAL_MAILBOX_PROVIDER = "imap"
EVAL_CREDENTIALS_REF = "eval/none"
"""A reference no adapter resolves: the evaluation mailbox is never fetched from or sent through."""

INSERT_EVAL_MAILBOX_SQL = """
    INSERT INTO mailbox (id, organization_id, provider, address, credentials_ref)
    VALUES ($1, $2, $3, $4, $5)
"""


async def create_eval_mailbox(pool: asyncpg.Pool, *, organization_id: UUID, address: str) -> UUID:
    """Insert an evaluation mailbox for one organization and return its id.

    Args:
        pool: Database pool of the pipeline the feeder drives.
        organization_id: Tenant that owns the mailbox (the feeder creates one per case).
        address: Mailbox address, unique per organization; the case email's ``To`` header.

    Returns:
        The new mailbox id.

    Raises:
        asyncpg.UniqueViolationError: If the organization already has a mailbox at ``address``.
    """
    mailbox_id = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            INSERT_EVAL_MAILBOX_SQL,
            mailbox_id,
            organization_id,
            EVAL_MAILBOX_PROVIDER,
            address,
            EVAL_CREDENTIALS_REF,
        )
    return mailbox_id
