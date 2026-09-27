"""BackgroundLoop start/stop semantics (RA.10, R20.8, R2.10)."""

from __future__ import annotations

import asyncio
import time

from prometheus_client import CollectorRegistry

from packages.adapters.fake import FakeProviderAdapter
from packages.db.mailbox import InMemoryMailboxStore
from packages.db.subscription import InMemorySubscriptionStore
from packages.observability.metrics import create_pipeline_metrics
from services.mail_connector.background import BackgroundLoop
from services.mail_connector.renewal import SubscriptionRenewalJob


async def test_stop_signals_event_and_waits() -> None:
    started = asyncio.Event()

    async def run(stop: asyncio.Event) -> None:
        started.set()
        await stop.wait()

    loop = BackgroundLoop("t", run, stop_timeout_s=1.0)
    await loop.start()
    await asyncio.wait_for(started.wait(), 1.0)
    await loop.stop()
    assert not loop.running


async def test_stop_cancels_loop_that_ignores_event() -> None:
    cancelled = asyncio.Event()

    async def run(stop: asyncio.Event) -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    loop = BackgroundLoop("t", run, stop_timeout_s=0.05)
    await loop.start()
    await asyncio.sleep(0)
    await loop.stop()
    assert cancelled.is_set() and not loop.running


async def test_renewal_loop_stops_promptly() -> None:
    job = SubscriptionRenewalJob(
        subscription_store=InMemorySubscriptionStore(),
        mailbox_store=InMemoryMailboxStore(),
        adapter_resolver=lambda _: FakeProviderAdapter(),
        metrics=create_pipeline_metrics(registry=CollectorRegistry()),
    )
    loop = BackgroundLoop(
        "renewal", lambda s: job.run_loop(stop_event=s, interval_seconds=3600.0), 2.0
    )
    await loop.start()
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    await loop.stop()
    assert time.monotonic() - t0 < 1.0
