"""Retry ladder, backoff, and dead-letter queue coordinator.

Requirements:
- R19.5: Transition GENERATING -> RETRY_PENDING -> GENERATING with exponential backoff and jitter.
- R19.6: Transition FAILED -> DEAD_LETTER when retry limit exceeded.
- R3.5: Preserve original routing key, original exchange, attempt, and failure reason in DLQ.
- R18.2: Support failure states RETRY_PENDING, FAILED, DEAD_LETTER.
- R18.4, R18.5: Atomically record ProcessingEvent on transition in the same transaction.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from packages.broker.backoff import calculate_exponential_backoff, resolve_retry_tier_delay
from packages.broker.envelope import JobEnvelope
from packages.core.settings import RetryLadderSettings
from packages.domain.state_machine import JobState
from packages.observability.metrics import get_metrics

if TYPE_CHECKING:
    from packages.broker.publisher import MessagePublisher
    from packages.db.job import JobStoreProtocol

logger = logging.getLogger(__name__)


def is_valid_uuid(val: str | None) -> bool:
    """Check if string is a valid UUID."""
    if not val:
        return False
    try:
        UUID(val)
        return True
    except (ValueError, TypeError):
        return False


async def handle_job_recovery(
    envelope: JobEnvelope,
    job_store: JobStoreProtocol | None = None,
) -> None:
    """Transition job from RETRY_PENDING back to GENERATING on redelivery (R19.5, R18.2).

    Parameters
    ----------
    envelope : JobEnvelope
        Deserialized job envelope.
    job_store : JobStoreProtocol | None
        Optional durable store for job entity and event persistence.
    """
    if job_store is None or not is_valid_uuid(envelope.job_id):
        return

    try:
        job = await job_store.get_job(envelope.organization_id, UUID(envelope.job_id))
        if job and job.state == JobState.RETRY_PENDING.value:
            await job_store.transition_job_state(
                organization_id=envelope.organization_id,
                job_id=UUID(envelope.job_id),
                target_state=JobState.GENERATING,
                payload={
                    "resumed_at_attempt": envelope.attempt,
                    "idempotency_key": envelope.idempotency_key,
                },
                message_id=UUID(envelope.message_id)
                if is_valid_uuid(envelope.message_id)
                else None,
                thread_id=UUID(envelope.thread_id) if is_valid_uuid(envelope.thread_id) else None,
            )
            logger.info(
                "Job %s recovered: transitioned RETRY_PENDING -> GENERATING (attempt %d)",
                envelope.job_id,
                envelope.attempt,
            )
    except Exception as err:
        logger.warning(
            "Failed to transition job %s from RETRY_PENDING to GENERATING on recovery: %s",
            envelope.job_id,
            err,
        )


async def handle_job_transient_failure(
    envelope: JobEnvelope,
    exception: Exception,
    publisher: MessagePublisher,
    retry_settings: RetryLadderSettings,
    origin_exchange: str,
    origin_routing_key: str,
    queue_name: str,
    job_store: JobStoreProtocol | None = None,
) -> JobEnvelope:
    """Handle a transient failure: transition to RETRY_PENDING and publish to retry ladder.

    Requirements: R19.5, R18.2.

    Parameters
    ----------
    envelope : JobEnvelope
        Current job envelope.
    exception : Exception
        Transient error that was raised.
    publisher : MessagePublisher
        Publisher instance for AMQP transmission.
    retry_settings : RetryLadderSettings
        Retry configuration thresholds and backoff intervals.
    origin_exchange : str
        Original exchange consumed from.
    origin_routing_key : str
        Original routing key to return to after TTL.
    queue_name : str
        Name of the queue being consumed.
    job_store : JobStoreProtocol | None
        Optional durable store for state transitions.

    Returns
    -------
    JobEnvelope
        Updated envelope with incremented attempt count.
    """
    next_attempt = envelope.attempt + 1
    delay_s = resolve_retry_tier_delay(next_attempt, retry_settings)
    jittered_s = calculate_exponential_backoff(
        attempt=next_attempt,
        base_s=retry_settings.backoff_base_s,
        factor=retry_settings.backoff_factor,
        max_s=retry_settings.max_backoff_s,
        jitter_mode=retry_settings.jitter_mode,  # type: ignore[arg-type]
    )

    reason = f"{type(exception).__name__}: {exception}"

    # 1. Atomically record state transition to RETRY_PENDING (R19.5, R18.2, R18.4)
    if job_store is not None and is_valid_uuid(envelope.job_id):
        try:
            job = await job_store.get_job(envelope.organization_id, UUID(envelope.job_id))
            if job and job.state in (
                JobState.GENERATING.value,
                JobState.FAILED.value,
            ):
                await job_store.transition_job_state(
                    organization_id=envelope.organization_id,
                    job_id=UUID(envelope.job_id),
                    target_state=JobState.RETRY_PENDING,
                    error_message=reason,
                    payload={
                        "attempt": next_attempt,
                        "delay_s": delay_s,
                        "jittered_delay_s": round(jittered_s, 2),
                        "error": str(exception),
                        "error_type": type(exception).__name__,
                        "origin_routing_key": origin_routing_key,
                    },
                    message_id=UUID(envelope.message_id)
                    if is_valid_uuid(envelope.message_id)
                    else None,
                    thread_id=UUID(envelope.thread_id)
                    if is_valid_uuid(envelope.thread_id)
                    else None,
                )
                logger.info(
                    "Job %s transitioned to RETRY_PENDING (attempt %d, delay %ds)",
                    envelope.job_id,
                    next_attempt,
                    delay_s,
                )
        except Exception as err:
            logger.warning(
                "Failed to update job %s state to RETRY_PENDING: %s", envelope.job_id, err
            )

    # 2. Publish to retry ladder with TTL
    retry_envelope = envelope.model_copy(update={"attempt": next_attempt})
    effective_exchange = origin_exchange or publisher.settings.exchange_email_route

    await publisher.publish_to_retry(
        envelope=retry_envelope,
        tier_delay_s=delay_s,
        origin_exchange=effective_exchange,
        origin_routing_key=origin_routing_key,
        failure_reason=reason,
    )

    # 3. Record Prometheus retry metric (R21.4)
    metrics = get_metrics()
    metrics.retry_jobs_total.labels(queue=queue_name, tier=f"{delay_s}s").inc()

    logger.info(
        "Job %s routed to retry tier %ds (attempt %d/%d): %s",
        envelope.job_id,
        delay_s,
        next_attempt,
        retry_settings.max_retries,
        reason,
    )
    return retry_envelope


async def handle_job_terminal_failure(
    envelope: JobEnvelope,
    exception: Exception,
    publisher: MessagePublisher,
    origin_exchange: str,
    origin_routing_key: str,
    queue_name: str,
    job_store: JobStoreProtocol | None = None,
) -> None:
    """Handle a terminal failure or retry exhaustion: transition FAILED -> DEAD_LETTER.

    Requirements: R19.6, R3.5, R18.2.

    Parameters
    ----------
    envelope : JobEnvelope
        Failed job envelope.
    exception : Exception
        Terminal error causing failure or exhaustion.
    publisher : MessagePublisher
        Publisher instance.
    origin_exchange : str
        Original exchange consumed from.
    origin_routing_key : str
        Original destination routing key.
    queue_name : str
        Queue being consumed.
    job_store : JobStoreProtocol | None
        Optional durable store for state transitions.
    """
    reason = f"{type(exception).__name__}: {exception}"

    # 1. Atomically record state transition FAILED -> DEAD_LETTER (R19.6, R18.2, R18.4)
    if job_store is not None and is_valid_uuid(envelope.job_id):
        try:
            job = await job_store.get_job(envelope.organization_id, UUID(envelope.job_id))
            if job:
                # Transition to FAILED first if not already in FAILED state
                if job.state != JobState.FAILED.value and job.state != JobState.DEAD_LETTER.value:
                    await job_store.transition_job_state(
                        organization_id=envelope.organization_id,
                        job_id=UUID(envelope.job_id),
                        target_state=JobState.FAILED,
                        error_message=reason,
                        payload={
                            "reason": reason,
                            "attempt": envelope.attempt,
                            "origin_routing_key": origin_routing_key,
                        },
                        message_id=UUID(envelope.message_id)
                        if is_valid_uuid(envelope.message_id)
                        else None,
                        thread_id=UUID(envelope.thread_id)
                        if is_valid_uuid(envelope.thread_id)
                        else None,
                    )
                # Then transition to DEAD_LETTER (R19.6)
                if job.state != JobState.DEAD_LETTER.value:
                    await job_store.transition_job_state(
                        organization_id=envelope.organization_id,
                        job_id=UUID(envelope.job_id),
                        target_state=JobState.DEAD_LETTER,
                        error_message=reason,
                        payload={
                            "reason": reason,
                            "attempt": envelope.attempt,
                            "origin_routing_key": origin_routing_key,
                            "terminal": True,
                        },
                        message_id=UUID(envelope.message_id)
                        if is_valid_uuid(envelope.message_id)
                        else None,
                        thread_id=UUID(envelope.thread_id)
                        if is_valid_uuid(envelope.thread_id)
                        else None,
                    )
                logger.warning(
                    "Job %s transitioned FAILED -> DEAD_LETTER after %d attempts",
                    envelope.job_id,
                    envelope.attempt,
                )
        except Exception as err:
            logger.warning("Failed to update job %s state to DEAD_LETTER: %s", envelope.job_id, err)

    # 2. Publish to terminal DLQ exchange with required diagnostic headers (R3.5)
    await publisher.publish_to_dead_letter(
        envelope=envelope,
        failure_reason=reason,
        origin_routing_key=origin_routing_key,
        origin_exchange=origin_exchange,
    )

    # 3. Record Prometheus dead-letter metric (R21.4)
    metrics = get_metrics()
    metrics.failed_jobs_total.labels(
        queue=queue_name,
        job_type=envelope.job_type,
        error_type=type(exception).__name__,
    ).inc()

    logger.error(
        "Job %s routed to dead-letter exchange (attempt %d): %s",
        envelope.job_id,
        envelope.attempt,
        reason,
    )
