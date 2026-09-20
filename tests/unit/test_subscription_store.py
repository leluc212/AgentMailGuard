"""Unit tests for SubscriptionStore protocol and InMemorySubscriptionStore (R1.1, R2.10, R5.3)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from packages.db.subscription import InMemorySubscriptionStore, SubscriptionStore
from packages.domain.entities import Subscription


@pytest.mark.asyncio
async def test_in_memory_subscription_store_protocol() -> None:
    """Verify InMemorySubscriptionStore conforms to SubscriptionStore protocol."""
    store = InMemorySubscriptionStore()
    assert isinstance(store, SubscriptionStore)


@pytest.mark.asyncio
async def test_save_and_get_subscription() -> None:
    """Verify persisting and retrieving a subscription by id and mailbox_id."""
    store = InMemorySubscriptionStore()
    org_id = uuid4()
    mbx_id = uuid4()
    now = datetime.now(UTC)
    expires = now + timedelta(days=3)

    sub = Subscription(
        mailbox_id=mbx_id,
        subscription_id="sub-123",
        expires_at=expires,
        provider="fake",
        resource="inbox",
        client_state="secret-123",
        organization_id=org_id,
    )
    await store.save(sub)

    # Get by subscription_id
    retrieved = await store.get("sub-123")
    assert retrieved is not None
    assert retrieved.subscription_id == "sub-123"
    assert retrieved.mailbox_id == mbx_id
    assert retrieved.organization_id == org_id
    assert retrieved.resource == "inbox"
    assert retrieved.client_state == "secret-123"

    # Get by mailbox_id
    by_mbx = await store.get_by_mailbox(mbx_id)
    assert by_mbx is not None
    assert by_mbx.subscription_id == "sub-123"

    # Org scoping check
    wrong_org = uuid4()
    assert await store.get("sub-123", organization_id=wrong_org) is None
    assert await store.get_by_mailbox(mbx_id, organization_id=wrong_org) is None
    assert await store.get("sub-123", organization_id=org_id) is not None


@pytest.mark.asyncio
async def test_list_expiring_subscriptions() -> None:
    """Verify list_expiring finds subscriptions expiring before cutoff and orders them."""
    store = InMemorySubscriptionStore()
    now = datetime.now(UTC)

    # Sub 1: expires in 2 hours
    sub1 = Subscription(
        mailbox_id=uuid4(),
        subscription_id="sub-1",
        expires_at=now + timedelta(hours=2),
        provider="fake",
    )
    # Sub 2: expires in 12 hours
    sub2 = Subscription(
        mailbox_id=uuid4(),
        subscription_id="sub-2",
        expires_at=now + timedelta(hours=12),
        provider="fake",
    )
    # Sub 3: expires in 48 hours (not expiring within 24 hours)
    sub3 = Subscription(
        mailbox_id=uuid4(),
        subscription_id="sub-3",
        expires_at=now + timedelta(hours=48),
        provider="fake",
    )

    await store.save(sub1)
    await store.save(sub2)
    await store.save(sub3)

    cutoff = now + timedelta(hours=24)
    expiring = await store.list_expiring(before=cutoff)

    assert len(expiring) == 2
    assert expiring[0].subscription_id == "sub-1"
    assert expiring[1].subscription_id == "sub-2"

    # Test limit
    expiring_limited = await store.list_expiring(before=cutoff, limit=1)
    assert len(expiring_limited) == 1
    assert expiring_limited[0].subscription_id == "sub-1"


@pytest.mark.asyncio
async def test_record_renewal_outcome() -> None:
    """Verify recording renewal outcomes updates status, error, and new expiry."""
    store = InMemorySubscriptionStore()
    now = datetime.now(UTC)
    org_id = uuid4()
    sub = Subscription(
        mailbox_id=uuid4(),
        subscription_id="sub-renew-1",
        expires_at=now + timedelta(hours=2),
        provider="fake",
        organization_id=org_id,
    )
    await store.save(sub)

    new_expires = now + timedelta(days=7)
    await store.record_renewal_outcome(
        subscription_id="sub-renew-1",
        status="success",
        new_expires_at=new_expires,
        error=None,
    )

    updated = await store.get("sub-renew-1")
    assert updated is not None
    assert updated.last_renewal_status == "success"
    assert updated.last_error is None
    assert updated.expires_at == new_expires
    assert updated.last_renewed_at is not None

    # Record failure
    await store.record_renewal_outcome(
        subscription_id="sub-renew-1",
        status="needs_reauth",
        error="Token revoked",
    )
    failed = await store.get("sub-renew-1")
    assert failed is not None
    assert failed.last_renewal_status == "needs_reauth"
    assert failed.last_error == "Token revoked"
