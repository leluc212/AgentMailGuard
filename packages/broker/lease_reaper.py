"""Lease reaper for recovering stuck processing jobs (R19.8, design.md §9).

Periodically sweeps the database for jobs stuck past their lease_expires_at,
transitioning them back to a retryable state (RETRY_PENDING) if attempts remain,
or to DEAD_LETTER if attempts are exhausted. Emits Prometheus metrics and optionally
re-enqueues jobs to RabbitMQ.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from packages.broker.envelope import JobEnvelope
from packages.core.settings import LeaseReaperSettings
from packages.domain.entities import Job, ProcessingEvent
from packages.observability.metrics import PipelineMetrics, get_metrics

if TYPE_CHECKING:
    from packages.broker.publisher import MessagePublisher
    from packages.db.job import JobStoreProtocol

logger = logging.getLogger(__name__)


class LeaseReaper:
    """Background service to detect and reclaim stuck processing jobs past lease expiry (R19.8)."""

    def __init__(
        self,
        job_store: JobStoreProtocol,
        settings: LeaseReaperSettings | None = None,
        publisher: MessagePublisher | None = None,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        self.job_store = job_store
        self.settings = settings or LeaseReaperSettings()
        self.publisher = publisher
        self.metrics = metrics or get_metrics()
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def reap_once(
        self, organization_id: UUID | str | None = None
    ) -> list[tuple[Job, ProcessingEvent, str]]:
        """Perform a single sweep to reclaim expired leases."""
        unleased_timeout = (
            self.settings.lease_timeout_s if self.settings.reap_stuck_unleased else None
        )
        try:
            reaped = await self.job_store.reap_expired_jobs(
                batch_size=self.settings.batch_size,
                unleased_timeout_s=unleased_timeout,
                organization_id=organization_id,
            )
        except Exception as err:
            logger.error("Error during lease reaper sweep: %s", err, exc_info=True)
            return []

        for job, event, action in reaped:
            # Emit Prometheus counter (R21.4)
            prev_state = (
                str(event.payload.get("initial_state"))
                if event.payload and "initial_state" in event.payload
                else event.state_from or "UNKNOWN"
            )
            self.metrics.reaped_leases_total.labels(action=action, state=prev_state).inc()

            # Publish to broker if publisher is attached
            if self.publisher is not None:
                await self._publish_reaped_job(job=job, event=event, action=action)

        if reaped:
            logger.info("Lease reaper sweep completed: reclaimed %d stuck jobs", len(reaped))

        return reaped

    async def _publish_reaped_job(
        self,
        job: Job,
        event: ProcessingEvent,
        action: str,
    ) -> None:
        """Route reaped job envelope to retry ladder or dead-letter queue."""
        assert self.publisher is not None

        envelope = JobEnvelope(
            job_id=str(job.id),
            organization_id=str(job.organization_id),
            message_id=str(job.message_id) if job.message_id else "",
            thread_id=str(job.thread_id) if job.thread_id else "",
            job_type=job.job_type,
            attempt=job.attempt,
            idempotency_key=job.idempotency_key,
            trace_id=job.trace_id or uuid4().hex,
        )

        target_queue = job.queue_name or "email.general_inquiry.normal"

        try:
            if action == "reclaimed":
                # Route to retry tier with TTL backoff
                await self.publisher.publish_to_retry(
                    envelope=envelope,
                    tier_delay_s=30,
                    origin_exchange=self.publisher.settings.exchange_email_route,
                    origin_routing_key=target_queue,
                    failure_reason=job.last_error or f"Lease expired in {event.state_from}",
                )
                logger.info(
                    "Reaped job %s published to retry tier (attempt %d)", job.id, job.attempt
                )
            elif action == "dead_letter":
                # Route to dead letter exchange
                await self.publisher.publish_to_dead_letter(
                    envelope=envelope,
                    failure_reason=job.last_error or "Lease expired and max attempts exceeded",
                    origin_exchange=self.publisher.settings.exchange_email_route,
                    origin_routing_key=target_queue,
                )
                logger.warning(
                    "Reaped job %s published to DLQ (attempts exhausted: %d)",
                    job.id,
                    job.attempt,
                )
        except Exception as pub_err:
            logger.error(
                "Failed to publish reaped job %s to broker: %s", job.id, pub_err, exc_info=True
            )

    async def _run_loop(self) -> None:
        """Periodic reaper loop."""
        while self._running:
            try:
                await self.reap_once()
            except Exception as err:
                logger.error("Unexpected error in lease reaper loop: %s", err)

            try:
                await asyncio.sleep(self.settings.reaper_interval_s)
            except asyncio.CancelledError:
                break

    def start(self) -> None:
        """Start background reaper loop."""
        if self._running:
            logger.warning("Lease reaper already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Lease reaper started (interval=%.1fs, batch_size=%d, timeout=%ds)",
            self.settings.reaper_interval_s,
            self.settings.batch_size,
            self.settings.lease_timeout_s,
        )

    async def stop(self) -> None:
        """Stop background reaper loop gracefully."""
        if not self._running:
            return
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        logger.info("Lease reaper stopped")
