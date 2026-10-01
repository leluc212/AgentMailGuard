"""Subscription store interface and backends (R1.1, R2.2, R2.10, R5.3).

Provides subscription persistence, lookup, expiration querying, and outcome recording.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import asyncpg

from packages.domain.entities import Subscription

logger = logging.getLogger(__name__)


@runtime_checkable
class SubscriptionStore(Protocol):
    """Protocol for provider push/webhook subscription persistence operations."""

    async def get(
        self, subscription_id: str, organization_id: UUID | str | None = None
    ) -> Subscription | None:
        """Fetch subscription entity by subscription_id."""
        ...

    async def get_by_mailbox(
        self, mailbox_id: UUID | str, organization_id: UUID | str | None = None
    ) -> Subscription | None:
        """Fetch active subscription for a mailbox."""
        ...

    async def list_expiring(self, before: datetime, limit: int = 100) -> list[Subscription]:
        """Fetch subscriptions expiring on or before the given cutoff timestamp."""
        ...

    async def save(self, subscription: Subscription) -> None:
        """Persist or upsert subscription."""
        ...

    async def record_renewal_outcome(
        self,
        subscription_id: str,
        status: str,
        new_expires_at: datetime | None = None,
        error: str | None = None,
        organization_id: UUID | str | None = None,
    ) -> None:
        """Record renewal attempt outcome and updated expiry."""
        ...


class PostgresSubscriptionStore(SubscriptionStore):
    """PostgreSQL implementation of SubscriptionStore (R5.3, R2.10)."""

    def __init__(self, pool: asyncpg.Pool[Any]) -> None:
        self.pool = pool

    def _row_to_entity(self, row: asyncpg.Record) -> Subscription:
        return Subscription(
            mailbox_id=row["mailbox_id"],
            subscription_id=row["subscription_id"],
            expires_at=row["expires_at"],
            provider=row["provider"],
            resource=row["resource"],
            client_state=row["client_state"],
            organization_id=row["organization_id"],
            last_renewed_at=row["last_renewed_at"],
            last_renewal_status=row["last_renewal_status"],
            last_error=row["last_error"],
        )

    async def get(
        self, subscription_id: str, organization_id: UUID | str | None = None
    ) -> Subscription | None:
        """Fetch subscription entity by subscription_id."""
        org_uuid = UUID(str(organization_id)) if organization_id else None
        query = """
            SELECT organization_id, mailbox_id, subscription_id, provider, resource,
                   client_state, expires_at, last_renewed_at, last_renewal_status, last_error
            FROM mailbox_subscription
            WHERE subscription_id = $1
              AND ($2::uuid IS NULL OR organization_id = $2::uuid);
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, subscription_id, org_uuid)
            if row is None:
                return None
            return self._row_to_entity(row)

    async def get_by_mailbox(
        self, mailbox_id: UUID | str, organization_id: UUID | str | None = None
    ) -> Subscription | None:
        """Fetch active subscription for a mailbox."""
        mbx_uuid = UUID(str(mailbox_id))
        org_uuid = UUID(str(organization_id)) if organization_id else None
        query = """
            SELECT organization_id, mailbox_id, subscription_id, provider, resource,
                   client_state, expires_at, last_renewed_at, last_renewal_status, last_error
            FROM mailbox_subscription
            WHERE mailbox_id = $1
              AND ($2::uuid IS NULL OR organization_id = $2::uuid);
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, mbx_uuid, org_uuid)
            if row is None:
                return None
            return self._row_to_entity(row)

    async def list_expiring(self, before: datetime, limit: int = 100) -> list[Subscription]:
        """Fetch subscriptions expiring on or before the given cutoff timestamp (R2.10)."""
        query = """
            SELECT organization_id, mailbox_id, subscription_id, provider, resource,
                   client_state, expires_at, last_renewed_at, last_renewal_status, last_error
            FROM mailbox_subscription
            WHERE expires_at <= $1
            ORDER BY expires_at ASC
            LIMIT $2;
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, before, limit)
            return [self._row_to_entity(r) for r in rows]

    async def save(self, subscription: Subscription) -> None:
        """Persist or upsert subscription with tenant scoping (R5.3)."""
        mbx_uuid = UUID(str(subscription.mailbox_id))
        org_uuid = UUID(str(subscription.organization_id)) if subscription.organization_id else None

        query = """
            INSERT INTO mailbox_subscription (
                organization_id, mailbox_id, subscription_id, provider, resource,
                client_state, expires_at, last_renewed_at, last_renewal_status,
                last_error, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now())
            ON CONFLICT (organization_id, mailbox_id) DO UPDATE SET
                subscription_id = EXCLUDED.subscription_id,
                provider = EXCLUDED.provider,
                resource = EXCLUDED.resource,
                client_state = EXCLUDED.client_state,
                expires_at = EXCLUDED.expires_at,
                last_renewed_at = COALESCE(
                    EXCLUDED.last_renewed_at, mailbox_subscription.last_renewed_at
                ),
                last_renewal_status = COALESCE(
                    EXCLUDED.last_renewal_status, mailbox_subscription.last_renewal_status
                ),
                last_error = COALESCE(
                    EXCLUDED.last_error, mailbox_subscription.last_error
                ),
                updated_at = now();
        """
        async with self.pool.acquire() as conn:
            if org_uuid is None:
                org_row = await conn.fetchrow(
                    "SELECT organization_id FROM mailbox WHERE id = $1", mbx_uuid
                )
                if org_row:
                    org_uuid = org_row["organization_id"]
                else:
                    raise ValueError(
                        f"Cannot save subscription: missing org_id and mailbox {mbx_uuid} not found"
                    )

            await conn.execute(
                query,
                org_uuid,
                mbx_uuid,
                subscription.subscription_id,
                subscription.provider,
                subscription.resource,
                subscription.client_state,
                subscription.expires_at,
                subscription.last_renewed_at,
                subscription.last_renewal_status,
                subscription.last_error,
            )

    async def record_renewal_outcome(
        self,
        subscription_id: str,
        status: str,
        new_expires_at: datetime | None = None,
        error: str | None = None,
        organization_id: UUID | str | None = None,
    ) -> None:
        """Record renewal attempt outcome and updated expiry in PostgreSQL (R2.10)."""
        org_uuid = UUID(str(organization_id)) if organization_id else None
        query = """
            UPDATE mailbox_subscription
            SET last_renewed_at = now(),
                last_renewal_status = $2,
                last_error = $3,
                expires_at = COALESCE($4, expires_at),
                updated_at = now()
            WHERE subscription_id = $1
              AND ($5::uuid IS NULL OR organization_id = $5::uuid);
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, subscription_id, status, error, new_expires_at, org_uuid)


class InMemorySubscriptionStore(SubscriptionStore):
    """In-memory implementation of SubscriptionStore for unit tests."""

    def __init__(self, initial_subscriptions: list[Subscription] | None = None) -> None:
        self._subscriptions: dict[str, Subscription] = {}
        self._lock = asyncio.Lock()
        if initial_subscriptions:
            for sub in initial_subscriptions:
                self._subscriptions[sub.subscription_id] = sub

    def _clone(self, s: Subscription) -> Subscription:
        return Subscription(
            mailbox_id=s.mailbox_id,
            subscription_id=s.subscription_id,
            expires_at=s.expires_at,
            provider=s.provider,
            resource=s.resource,
            client_state=s.client_state,
            organization_id=s.organization_id,
            last_renewed_at=s.last_renewed_at,
            last_renewal_status=s.last_renewal_status,
            last_error=s.last_error,
        )

    async def get(
        self, subscription_id: str, organization_id: UUID | str | None = None
    ) -> Subscription | None:
        async with self._lock:
            sub = self._subscriptions.get(subscription_id)
            if sub is None:
                return None
            if organization_id and str(sub.organization_id) != str(organization_id):
                return None
            return self._clone(sub)

    async def get_by_mailbox(
        self, mailbox_id: UUID | str, organization_id: UUID | str | None = None
    ) -> Subscription | None:
        async with self._lock:
            for sub in self._subscriptions.values():
                if str(sub.mailbox_id) == str(mailbox_id):
                    if organization_id and str(sub.organization_id) != str(organization_id):
                        continue
                    return self._clone(sub)
            return None

    async def list_expiring(self, before: datetime, limit: int = 100) -> list[Subscription]:
        async with self._lock:
            results: list[Subscription] = []
            for sub in self._subscriptions.values():
                if sub.expires_at <= before:
                    results.append(self._clone(sub))
            results.sort(key=lambda s: s.expires_at)
            return results[:limit]

    async def save(self, subscription: Subscription) -> None:
        async with self._lock:
            # Check if matching (org, mailbox) already exists and replace
            matched_key = None
            for key, s in self._subscriptions.items():
                same_mailbox = str(s.mailbox_id) == str(subscription.mailbox_id)
                same_org = (
                    subscription.organization_id is None
                    or s.organization_id is None
                    or str(s.organization_id) == str(subscription.organization_id)
                )
                if same_mailbox and same_org:
                    matched_key = key
                    break
            if matched_key and matched_key != subscription.subscription_id:
                del self._subscriptions[matched_key]

            self._subscriptions[subscription.subscription_id] = self._clone(subscription)

    async def record_renewal_outcome(
        self,
        subscription_id: str,
        status: str,
        new_expires_at: datetime | None = None,
        error: str | None = None,
        organization_id: UUID | str | None = None,
    ) -> None:
        async with self._lock:
            sub = self._subscriptions.get(subscription_id)
            if sub is not None:
                if organization_id and str(sub.organization_id) != str(organization_id):
                    return
                sub.last_renewed_at = datetime.now(UTC)
                sub.last_renewal_status = status
                sub.last_error = error
                if new_expires_at is not None:
                    sub.expires_at = new_expires_at
