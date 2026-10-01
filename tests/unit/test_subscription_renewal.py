"""Unit tests for SubscriptionRenewalJob (R2.10, R1.5).

Verifies scheduled renewal of expiring provider subscriptions, outcome recording,
common error taxonomy translation, and automatic mailbox 'needs_reauth' transition
on AuthExpired to prevent spinning.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from packages.adapters.fake import FakeProviderAdapter
from packages.db.mailbox import InMemoryMailboxStore
from packages.db.subscription import InMemorySubscriptionStore
from packages.domain.entities import Mailbox, Subscription
from packages.observability.metrics import create_pipeline_metrics
from services.mail_connector.renewal import SubscriptionRenewalJob


@pytest.mark.asyncio
async def test_renewal_job_normal_cycle() -> None:
    """Verify expiring subscriptions are renewed and outcomes recorded (R2.10)."""
    sub_store = InMemorySubscriptionStore()
    mbx_store = InMemoryMailboxStore()
    metrics = create_pipeline_metrics()

    org_id = uuid4()
    mbx_id1 = uuid4()
    mbx_id2 = uuid4()
    now = datetime.now(UTC)

    mailbox1 = Mailbox(
        id=mbx_id1,
        organization_id=org_id,
        provider="fake",
        address="user1@example.com",
        status="active",
    )
    mailbox2 = Mailbox(
        id=mbx_id2,
        organization_id=org_id,
        provider="fake",
        address="user2@example.com",
        status="active",
    )
    mbx_store.add(mailbox1)
    mbx_store.add(mailbox2)

    # Sub 1 expires in 4 hours (within 24h threshold)
    sub1 = Subscription(
        mailbox_id=mbx_id1,
        subscription_id="sub-expiring",
        expires_at=now + timedelta(hours=4),
        provider="fake",
        organization_id=org_id,
    )
    # Sub 2 expires in 72 hours (outside 24h threshold)
    sub2 = Subscription(
        mailbox_id=mbx_id2,
        subscription_id="sub-future",
        expires_at=now + timedelta(hours=72),
        provider="fake",
        organization_id=org_id,
    )
    await sub_store.save(sub1)
    await sub_store.save(sub2)

    adapter = FakeProviderAdapter()

    job = SubscriptionRenewalJob(
        subscription_store=sub_store,
        mailbox_store=mbx_store,
        adapter_resolver=lambda _: adapter,
        metrics=metrics,
    )

    summary = await job.run_once(now=now)

    assert summary.total_evaluated == 1
    assert summary.succeeded == 1
    assert summary.failed == 0
    assert summary.needs_reauth == 0

    # Verify sub1 was renewed
    renewed = await sub_store.get("sub-expiring")
    assert renewed is not None
    assert renewed.last_renewal_status == "success"
    assert renewed.last_renewed_at is not None
    # FakeProviderAdapter extends expiry by 7 days
    assert renewed.expires_at > now + timedelta(days=6)

    # Verify sub2 was untouched
    future = await sub_store.get("sub-future")
    assert future is not None
    assert future.last_renewal_status is None


@pytest.mark.asyncio
async def test_renewal_job_auth_expired_halts_and_marks_needs_reauth() -> None:
    """Verify AuthExpired transitions mailbox to needs_reauth without spinning (R1.5, R2.10)."""
    sub_store = InMemorySubscriptionStore()
    mbx_store = InMemoryMailboxStore()
    metrics = create_pipeline_metrics()

    org_id = uuid4()
    mbx_id = uuid4()
    now = datetime.now(UTC)

    mailbox = Mailbox(
        id=mbx_id,
        organization_id=org_id,
        provider="fake",
        address="revoked@example.com",
        status="active",
    )
    mbx_store.add(mailbox)

    sub = Subscription(
        mailbox_id=mbx_id,
        subscription_id="sub-auth-fail",
        expires_at=now + timedelta(hours=2),
        provider="fake",
        organization_id=org_id,
    )
    await sub_store.save(sub)

    # Configure FakeProviderAdapter to raise AuthExpired on renew_subscription
    adapter = FakeProviderAdapter()
    adapter.inject_auth_expired(calls=1, message="OAuth token revoked")

    job = SubscriptionRenewalJob(
        subscription_store=sub_store,
        mailbox_store=mbx_store,
        adapter_resolver=lambda _: adapter,
        metrics=metrics,
    )

    summary = await job.run_once(now=now)

    assert summary.total_evaluated == 1
    assert summary.succeeded == 0
    assert summary.needs_reauth == 1

    # Mailbox status must be transitioned to 'needs_reauth' (R1.5, R2.10)
    updated_mbx = await mbx_store.get(mbx_id)
    assert updated_mbx is not None
    assert updated_mbx.status == "needs_reauth"

    # Outcome recorded on subscription
    updated_sub = await sub_store.get("sub-auth-fail")
    assert updated_sub is not None
    assert updated_sub.last_renewal_status == "needs_reauth"
    assert "OAuth token revoked" in str(updated_sub.last_error)


@pytest.mark.asyncio
async def test_renewal_job_skips_needs_reauth_mailboxes() -> None:
    """Verify mailboxes already marked 'needs_reauth' are skipped without spinning."""
    sub_store = InMemorySubscriptionStore()
    mbx_store = InMemoryMailboxStore()
    metrics = create_pipeline_metrics()

    org_id = uuid4()
    mbx_id = uuid4()
    now = datetime.now(UTC)

    # Mailbox is already in needs_reauth status
    mailbox = Mailbox(
        id=mbx_id,
        organization_id=org_id,
        provider="fake",
        address="already_dead@example.com",
        status="needs_reauth",
    )
    mbx_store.add(mailbox)

    sub = Subscription(
        mailbox_id=mbx_id,
        subscription_id="sub-already-expired",
        expires_at=now + timedelta(hours=1),
        provider="fake",
        organization_id=org_id,
    )
    await sub_store.save(sub)

    called: list[Any] = []

    def mock_resolver(_: Mailbox) -> Any:
        called.append(True)
        return FakeProviderAdapter()

    job = SubscriptionRenewalJob(
        subscription_store=sub_store,
        mailbox_store=mbx_store,
        adapter_resolver=mock_resolver,
        metrics=metrics,
    )

    summary = await job.run_once(now=now)

    # Mailbox was evaluated, but skipped because status=='needs_reauth'
    assert summary.total_evaluated == 1
    assert summary.skipped == 1
    assert summary.succeeded == 0
    assert len(called) == 0, "Adapter should NOT be called for mailboxes in needs_reauth"


@pytest.mark.asyncio
async def test_renewal_job_handles_rate_limited() -> None:
    """Verify RateLimited records outcome and does NOT mark needs_reauth (R1.5, R1.6)."""
    sub_store = InMemorySubscriptionStore()
    mbx_store = InMemoryMailboxStore()
    metrics = create_pipeline_metrics()

    org_id = uuid4()
    mbx_id = uuid4()
    now = datetime.now(UTC)

    mailbox = Mailbox(
        id=mbx_id,
        organization_id=org_id,
        provider="fake",
        address="ratelimited@example.com",
        status="active",
    )
    mbx_store.add(mailbox)

    sub = Subscription(
        mailbox_id=mbx_id,
        subscription_id="sub-rate-limit",
        expires_at=now + timedelta(hours=3),
        provider="fake",
        organization_id=org_id,
    )
    await sub_store.save(sub)

    adapter = FakeProviderAdapter()
    adapter.inject_rate_limit(retry_after=120.0, calls=1, message="API limit")

    job = SubscriptionRenewalJob(
        subscription_store=sub_store,
        mailbox_store=mbx_store,
        adapter_resolver=lambda _: adapter,
        metrics=metrics,
    )

    summary = await job.run_once(now=now)

    assert summary.rate_limited == 1
    assert summary.succeeded == 0

    # Mailbox remains active
    mbx = await mbx_store.get(mbx_id)
    assert mbx is not None
    assert mbx.status == "active"

    # Outcome recorded
    updated_sub = await sub_store.get("sub-rate-limit")
    assert updated_sub is not None
    assert updated_sub.last_renewal_status == "rate_limited"


@pytest.mark.asyncio
async def test_renewal_job_handles_transient_error() -> None:
    """Verify Transient provider errors record failed status and keep mailbox active (R1.5)."""
    sub_store = InMemorySubscriptionStore()
    mbx_store = InMemoryMailboxStore()
    metrics = create_pipeline_metrics()

    org_id = uuid4()
    mbx_id = uuid4()
    now = datetime.now(UTC)

    mailbox = Mailbox(
        id=mbx_id,
        organization_id=org_id,
        provider="fake",
        address="transient@example.com",
        status="active",
    )
    mbx_store.add(mailbox)

    sub = Subscription(
        mailbox_id=mbx_id,
        subscription_id="sub-transient",
        expires_at=now + timedelta(hours=3),
        provider="fake",
        organization_id=org_id,
    )
    await sub_store.save(sub)

    adapter = FakeProviderAdapter()
    adapter.inject_transient_failure(message="503 Service Unavailable", calls=1)

    job = SubscriptionRenewalJob(
        subscription_store=sub_store,
        mailbox_store=mbx_store,
        adapter_resolver=lambda _: adapter,
        metrics=metrics,
    )

    summary = await job.run_once(now=now)

    assert summary.failed == 1
    assert summary.succeeded == 0

    mbx = await mbx_store.get(mbx_id)
    assert mbx is not None
    assert mbx.status == "active"

    updated_sub = await sub_store.get("sub-transient")
    assert updated_sub is not None
    assert updated_sub.last_renewal_status == "failed"


@pytest.mark.asyncio
async def test_renewal_job_run_loop_terminates_cleanly() -> None:
    """Verify run_loop stops when stop_event is set."""
    sub_store = InMemorySubscriptionStore()
    mbx_store = InMemoryMailboxStore()
    stop_event = asyncio.Event()

    job = SubscriptionRenewalJob(
        subscription_store=sub_store,
        mailbox_store=mbx_store,
        adapter_resolver=lambda _: FakeProviderAdapter(),
    )

    async def stopper() -> None:
        await asyncio.sleep(0.05)
        stop_event.set()

    task = asyncio.create_task(stopper())
    await job.run_loop(stop_event=stop_event, interval_seconds=10.0)
    await task
