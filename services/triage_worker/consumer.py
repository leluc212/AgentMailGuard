"""Triage worker consumer service (R6.1, R6.5, R6.6, R6.12–R6.15, R7.1, R7.3).

Consumes normalization output envelopes from `email.triage`, executes cascading
triage classification, evaluates early-exit gate logic, and dispatches actionable
messages to topic exchange `email.route` using routing key `email.<category>.<priority>`.
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection

from packages.broker.consumer import BaseConsumer, FatalError
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.routing import prepare_route_envelope
from packages.core.settings import BrokerSettings, RetryLadderSettings
from packages.db.job import JobStore
from packages.db.message import MessageStore
from packages.domain.entities import Job
from packages.domain.rules import EmailContext
from packages.domain.state_machine import JobState
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.triage_worker.cascade import CascadingTriageEngine
from services.triage_worker.gate import EarlyExitGate, GateAction, GateDecision

logger = logging.getLogger(__name__)


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


def _is_valid_uuid(val: str | None) -> bool:
    if not val:
        return False
    try:
        UUID(str(val))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


class TriageConsumer(BaseConsumer):
    """Consumer processing triage jobs, classifying urgency/category, and routing downstream."""

    def __init__(
        self,
        cascade: CascadingTriageEngine,
        gate: EarlyExitGate,
        publisher: MessagePublisher,
        broker_settings: BrokerSettings | None = None,
        retry_settings: RetryLadderSettings | None = None,
        prefetch_count: int | None = None,
        connection: AbstractRobustConnection | None = None,
        job_store: JobStore | None = None,
        message_store: MessageStore | None = None,
        route_exchange: str | None = None,
        shutdown_coordinator: GracefulShutdownCoordinator | None = None,
    ) -> None:
        settings = broker_settings or BrokerSettings()
        super().__init__(
            queue_name=settings.queue_triage,
            broker_settings=settings,
            retry_settings=retry_settings,
            prefetch_count=prefetch_count,
            connection=connection,
            shutdown_coordinator=shutdown_coordinator,
        )
        self.cascade = cascade
        self.gate = gate
        self.publisher = publisher
        self._publisher = publisher
        self.job_store = job_store
        self.message_store = message_store
        self.route_exchange = route_exchange or settings.exchange_email_route

    async def _resolve_email_context(
        self,
        envelope: JobEnvelope,
        org_id: UUID,
    ) -> EmailContext:
        """Resolve EmailContext from envelope payload or database message store."""
        payload = envelope.payload

        # 1. Direct email_context payload dictionary
        if "email_context" in payload and isinstance(payload["email_context"], dict):
            return EmailContext.from_dict(payload["email_context"])

        # 2. Raw fields in payload (e.g. subject, body_text, sender)
        if "subject" in payload or "body_text" in payload or "sender" in payload:
            return EmailContext.from_dict(payload)

        # 3. Fetch from message store if message_id is available
        if self.message_store and envelope.message_id and _is_valid_uuid(envelope.message_id):
            msg_uuid = _to_uuid(envelope.message_id)
            canonical_msg = await self.message_store.get_message(
                message_id=msg_uuid,
                organization_id=org_id,
            )
            if canonical_msg:
                return EmailContext.from_message(canonical_msg)

        # 4. Minimal fallback context
        return EmailContext(
            subject=payload.get("subject", ""),
            body_text=payload.get("body", ""),
            sender_email=payload.get("sender_email", ""),
        )

    async def _resolve_job(
        self,
        envelope: JobEnvelope,
        org_id: UUID,
    ) -> Job:
        """Fetch or synthesize a Job domain entity for state transitions."""
        job_id = _to_uuid(envelope.job_id) if _is_valid_uuid(envelope.job_id) else uuid4()

        if self.job_store:
            existing = await self.job_store.get_job(org_id, job_id)
            if existing:
                return existing

        msg_id = _to_uuid(envelope.message_id) if _is_valid_uuid(envelope.message_id) else None
        th_id = _to_uuid(envelope.thread_id) if _is_valid_uuid(envelope.thread_id) else None

        return Job(
            id=job_id,
            organization_id=org_id,
            idempotency_key=envelope.idempotency_key,
            job_type="triage",
            state=JobState.NORMALIZED,
            message_id=msg_id,
            thread_id=th_id,
            trace_id=envelope.trace_id,
            attempt=envelope.attempt,
        )

    async def process_job(
        self,
        envelope: JobEnvelope,
        raw_message: AbstractIncomingMessage,
    ) -> None:
        """Process an inbound triage job envelope to completion (R6.1, R7.1).

        1. Extract email context.
        2. Run cascading triage (rules -> ML -> LLM fallback).
        3. Evaluate early-exit gate.
        4. If actionable: publish envelope to topic exchange using routing key
           `email.<category>.<priority>`.
        """
        org_id = _to_uuid(envelope.organization_id)
        email_ctx = await self._resolve_email_context(envelope, org_id)
        job = await self._resolve_job(envelope, org_id)

        # Execute cascading triage
        try:
            cascade_result = await self.cascade.triage(
                email_ctx,
                organization_id=org_id,
            )
            classification = cascade_result.classification
        except Exception as err:
            logger.error(
                "Triage classification cascade failed for job %s: %s", envelope.job_id, err
            )
            raise FatalError(f"Classification failed: {err}") from err

        # Evaluate gate outcome
        decision: GateDecision
        if self.job_store:
            decision = await self.gate.evaluate_and_persist(
                job=job,
                classification=classification,
                job_store=self.job_store,
                trace_id=envelope.trace_id,
                message=email_ctx,
            )
        else:
            decision = self.gate.evaluate_decision(
                job=job,
                classification=classification,
                trace_id=envelope.trace_id,
                message=email_ctx,
            )

        # Route actionable email jobs downstream (R7.1)
        if decision.action in (GateAction.PROCEED_RAG, GateAction.PROCEED_NO_RAG):
            routing_key, route_envelope = prepare_route_envelope(envelope, decision.classification)
            await self.publisher.publish(
                exchange_name=self.route_exchange,
                routing_key=routing_key,
                envelope=route_envelope,
            )
            logger.info(
                "Actionable job %s published to '%s' with key '%s' (cat=%s, prio=%s)",
                envelope.job_id,
                self.route_exchange,
                routing_key,
                decision.classification.category,
                decision.classification.priority,
            )
        elif decision.action == GateAction.TEMPLATE_REPLY:
            logger.info(
                "Job %s routed to deterministic template reply; draft persisted",
                envelope.job_id,
            )
        elif decision.action == GateAction.EARLY_EXIT:
            logger.info(
                "Job %s early-exited (reply_required=false, category=%s)",
                envelope.job_id,
                decision.classification.category,
            )
