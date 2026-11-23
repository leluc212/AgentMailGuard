"""Helpers for the integration tests that roll migrations back in the shared test database."""

from __future__ import annotations

import asyncpg


async def release_demo_mailboxes(dsn: str) -> int:
    """Retype the `demo` mailboxes earlier tests seeded, so 0007 down can run.

    0007 down restores the pre-demo provider CHECK and refuses while a `demo` mailbox exists
    (task 9.3). Earlier tests (seed loader, demo ingest) leave such rows in this database; a
    test that rolls back past 0007 hands them back as `imap`. Returns the rows changed.
    """
    conn = await asyncpg.connect(dsn)
    try:
        exists = await conn.fetchval("SELECT to_regclass('public.mailbox') IS NOT NULL")
        if not exists:
            return 0
        status = await conn.execute("UPDATE mailbox SET provider = 'imap' WHERE provider = 'demo'")
        return int(status.split()[-1])
    finally:
        await conn.close()
