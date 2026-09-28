"""dispatch-worker consumer (tasks 6.5, 6.6): email.dispatch -> DispatchService.

The job carries its own claim (DRAFTED | RETRY_PENDING -> DISPATCHED inside the claim
transaction), so this consumer skips BaseConsumer's RETRY_PENDING -> GENERATING recovery and
takes no lease (design.md §5.8: the lease reaper skips DISPATCHED; redelivery is the
broker's). BaseConsumer keeps ack, the retry ladder (with the provider's Retry-After) and the
dead-letter path, which moves the job DISPATCHED -> FAILED -> DEAD_LETTER with the reason.

Two deliveries of one job never run together (R19.3): DispatchService holds a per-job lock,
and a delivery that finds it busy is re-published to the first retry tier with the same
attempt, then acked, without touching the job. Every dispatch failure first routes the job
to email.dispatch (R18.7), so an operator replay never regenerates the draft.
"""

from __future__ import annotations

import logging
from uuid import UUID

from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection

from packages.broker.consumer import BaseConsumer, FatalError
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import resolve_origin_exchange
from packages.core.settings import BrokerSettings, RetryLadderSettings
from packages.db.job import JobStore
from packages.dispatch.service import DispatchJobBusyError, DispatchService
from packages.observability.metrics import PipelineMetrics
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.dispatch_worker.failure_policy import (
    Disposition,
    classify_dispatch_failure,
    provider_retry_after,
)

logger = logging.getLogger(__name__)


class DispatchConsumer(BaseConsumer):
    """Consumes ``email.dispatch`` and dispatches each job's approved draft exactly once."""

    def __init__(
        self,
        queue_name: str,
        *,
        service: DispatchService,
        job_store: JobStore,
        broker_settings: BrokerSettings | None = None,
        retry_settings: RetryLadderSettings | None = None,
        prefetch_count: int | None = None,
        connection: AbstractRobustConnection | None = None,
        shutdown_coordinator: GracefulShutdownCoordinator | None = None,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        super().__init__(
            queue_name=queue_name,
            broker_settings=broker_settings,
            retry_settings=retry_settings,
            prefetch_count=prefetch_count,
            connection=connection,
            shutdown_coordinator=shutdown_coordinator,
            job_store=job_store,
            metrics=metrics,
        )
        self.service = service

    async def prepare_delivery(self, envelope: JobEnvelope) -> None:
        """No recovery and no lease: the dispatch claim owns the job's state (design §5.8)."""
        return None

    def retry_after_s(self, exc: Exception) -> float | None:
        """The provider's Retry-After picks the retry tier (R17.5)."""
        return provider_retry_after(exc)

    async def process_job(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage
    ) -> None:
        """Dispatch one job; route a failure to the retry ladder or the dead-letter queue."""
        try:
            organization_id = UUID(str(envelope.organization_id))
            job_id = UUID(str(envelope.job_id))
        except ValueError as err:
            raise FatalError(
                f"Dispatch envelope ids are not UUIDs: organization_id="
                f"{envelope.organization_id!r} job_id={envelope.job_id!r}"
            ) from err
        try:
            outcome = await self.service.dispatch(organization_id=organization_id, job_id=job_id)
        except DispatchJobBusyError as busy:
            await self._defer_busy(envelope, raw_message, str(busy))
            return
        except Exception as exc:
            await self._mark_dispatch_route(organization_id, job_id)
            decision = classify_dispatch_failure(exc)
            if decision.disposition is Disposition.DEAD_LETTER:
                logger.error("Dispatch of job %s failed permanently: %s", job_id, decision.reason)
                if isinstance(exc, FatalError):
                    raise
                raise FatalError(decision.reason) from exc
            logger.warning("Dispatch of job %s will be retried: %s", job_id, decision.reason)
            raise
        logger.info("Dispatch of job %s finished: %s", job_id, outcome.value)

    async def _defer_busy(
        self, envelope: JobEnvelope, raw_message: AbstractIncomingMessage, reason: str
    ) -> None:
        """Re-publish a delivery whose job another delivery is dispatching (R19.3).

        Same attempt number, first retry tier, no job transition: this delivery did no work.
        Returning normally lets BaseConsumer ack the original after the publish succeeded; if
        the publish raises, the delivery goes through the normal retry path instead.
        """
        if self._publisher is None:
            raise RuntimeError("consumer publisher is not initialised")
        await self._publisher.publish_to_retry(
            envelope,
            tier_delay_s=self.retry_settings.tier_1_delay_s,
            origin_exchange=resolve_origin_exchange(raw_message, self.broker_settings),
            origin_routing_key=raw_message.routing_key or self.queue_name,
            failure_reason=reason,
        )
        logger.info("Dispatch of job %s deferred: %s", envelope.job_id, reason)

    async def _mark_dispatch_route(self, organization_id: UUID, job_id: UUID) -> None:
        """Point the job's queue_name at email.dispatch before any retry or dead-letter."""
        try:
            await self.service.mark_dispatch_route(organization_id=organization_id, job_id=job_id)
        except Exception as route_err:  # never hide the dispatch failure behind this one
            logger.warning("Could not route job %s to email.dispatch: %s", job_id, route_err)
