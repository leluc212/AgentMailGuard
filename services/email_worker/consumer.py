"""Email normalization consumer service (R4.1, R4.7, R4.8, R4.9, R3.5, R5.8).

Consumes raw email normalization jobs from `email.normalize`, fetches raw MIME bytes from
object storage, executes MIME normalization, persists canonical domain records, and:
- If normalization fails: persists with normalization_failed=True, retains raw_object_key,
  and raises FatalError to route the job to dlx.email with failure headers (R4.9, R3.5).
- If normalization succeeds: persists message and attachments, and dispatches to
  email.triage (R6.1).
- If duplicate: acks and suppresses downstream dispatch (R4.8).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection

from packages.broker.consumer import BaseConsumer, FatalError
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.core.settings import BrokerSettings, RetryLadderSettings
from packages.core.storage import StorageProtocol
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.email_worker.normalizer import EmailNormalizer, NormalizationContext
from services.email_worker.persister import EmailPersister

logger = logging.getLogger(__name__)


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


class EmailNormalizationConsumer(BaseConsumer):
    """Consumer processing raw email payloads into canonical messages and attachments."""

    def __init__(
        self,
        normalizer: EmailNormalizer,
        persister: EmailPersister,
        storage_client: StorageProtocol,
        broker_settings: BrokerSettings | None = None,
        retry_settings: RetryLadderSettings | None = None,
        prefetch_count: int | None = None,
        connection: AbstractRobustConnection | None = None,
        publisher: MessagePublisher | None = None,
        triage_exchange: str | None = None,
        triage_routing_key: str | None = None,
        shutdown_coordinator: GracefulShutdownCoordinator | None = None,
    ) -> None:
        settings = broker_settings or BrokerSettings()
        super().__init__(
            queue_name=settings.queue_normalize,
            broker_settings=settings,
            retry_settings=retry_settings,
            prefetch_count=prefetch_count,
            connection=connection,
            shutdown_coordinator=shutdown_coordinator,
        )
        self.normalizer = normalizer
        self.persister = persister
        self.storage_client = storage_client
        self.triage_exchange = triage_exchange or settings.exchange_email_triage
        self.triage_routing_key = triage_routing_key or settings.queue_triage

        if publisher is not None:
            self._publisher = publisher

    async def process_job(
        self,
        envelope: JobEnvelope,
        raw_message: AbstractIncomingMessage,
    ) -> None:
        """Process a single normalization job envelope to completion.

        Performs:
        1. Raw MIME byte retrieval from object storage.
        2. Normalization & attachment/HTML offload.
        3. Database persistence with ON CONFLICT DO NOTHING (never discard).
        4. Dead-letter routing on normalization failure (R4.9, R3.5).
        5. Downstream triage dispatch on success (R6.1).
        """
        payload = envelope.payload
        raw_object_key = payload.get("raw_object_key")
        if not raw_object_key:
            raise FatalError(f"Job {envelope.job_id} payload missing required 'raw_object_key'")

        raw_bucket = payload.get("raw_bucket", "raw-emails")
        provider = payload.get("provider", "unknown")
        provider_message_id = (
            payload.get("provider_message_id") or envelope.message_id or f"msg-{envelope.job_id}"
        )
        provider_thread_id = payload.get("provider_thread_id") or envelope.thread_id

        org_id = _to_uuid(envelope.organization_id)
        mbx_id = _to_uuid(envelope.mailbox_id) if envelope.mailbox_id else uuid4()
        msg_id = (
            _to_uuid(envelope.message_id)
            if envelope.message_id and self._is_valid_uuid(envelope.message_id)
            else uuid4()
        )

        # 1. Fetch raw MIME bytes from object storage
        try:
            raw_mime = await self.storage_client.get_bytes(
                bucket=raw_bucket,
                key=raw_object_key,
            )
        except Exception as fetch_err:
            logger.error(
                "Failed to fetch raw MIME bytes (bucket=%s, key=%s): %s",
                raw_bucket,
                raw_object_key,
                fetch_err,
            )
            raise FatalError(
                f"Failed to fetch raw MIME payload from storage: {fetch_err}"
            ) from fetch_err

        # 2. Context setup and normalization
        context = NormalizationContext(
            organization_id=org_id,
            mailbox_id=mbx_id,
            message_id=msg_id,
            provider=provider,
            provider_message_id=provider_message_id,
            thread_id=provider_thread_id if provider_thread_id else None,
            raw_object_key=raw_object_key,
        )

        norm_result = await self.normalizer.normalize_and_offload(
            raw_mime=raw_mime,
            context=context,
            storage_client=self.storage_client,
        )

        # 3. Idempotent persistence into database (R4.8, R4.9 - never discard!)
        att_refs = [att.ref for att in norm_result.extracted_attachments]
        persist_result = await self.persister.persist(
            message=norm_result.message,
            attachments=att_refs,
            provider_thread_id=provider_thread_id,
        )

        # 4. Normalization failure handling (R4.9, R3.5)
        if norm_result.message.normalization_failed:
            logger.warning(
                "Message %s normalization failed; persisted with normalization_failed=True, "
                "dead-lettering job %s",
                provider_message_id,
                envelope.job_id,
            )
            raise FatalError(f"MIME normalization failed for message {provider_message_id}")

        # 5. Duplicate suppression (R4.8: treat as no-op success, emit no new job)
        if persist_result.is_duplicate or not persist_result.should_dispatch:
            logger.info(
                "Message %s is duplicate or already persisted; skipping triage dispatch",
                provider_message_id,
            )
            return

        # 6. Publish to downstream triage queue on normalization success
        if self._publisher is not None:
            triage_payload: dict[str, Any] = {
                "message_id": str(persist_result.message.message_id),
                "thread_id": str(persist_result.message.thread_id),
                "organization_id": str(org_id),
                "mailbox_id": str(mbx_id),
                "provider_message_id": provider_message_id,
                "direction": persist_result.message.direction,
                "received_at": persist_result.message.received_at.isoformat(),
            }
            triage_envelope = JobEnvelope(
                trace_id=envelope.trace_id,
                idempotency_key=f"triage:{org_id}:{mbx_id}:{provider_message_id}",
                organization_id=str(org_id),
                mailbox_id=str(mbx_id),
                message_id=str(persist_result.message.message_id),
                thread_id=str(persist_result.message.thread_id),
                job_type="triage_email",
                payload=triage_payload,
            )
            await self._publisher.publish(
                exchange_name=self.triage_exchange,
                routing_key=self.triage_routing_key,
                envelope=triage_envelope,
            )
            logger.info(
                "Dispatched triage job for message %s to exchange '%s' (routing_key='%s')",
                provider_message_id,
                self.triage_exchange,
                self.triage_routing_key,
            )

    @staticmethod
    def _is_valid_uuid(val: str) -> bool:
        try:
            UUID(str(val))
            return True
        except ValueError:
            return False
