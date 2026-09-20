"""Job envelope specification and message serialization (R3.1, R7.3).

Standard JSON envelope attached to every message in the asynchronous processing pipeline.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import aio_pika
from aio_pika.abc import AbstractIncomingMessage
from pydantic import BaseModel, Field


class JobEnvelope(BaseModel):
    """Authoritative asynchronous job envelope payload per specs/design.md §7.3.

    Carries tracking IDs, classification snapshots, attempt counts, and idempotency keys.
    """

    job_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()), description="Unique job execution identifier"
    )
    idempotency_key: str = Field(description="Deterministic idempotency token (R19.2)")
    job_type: str = Field(
        description="Pipeline job type (sync_mailbox, normalize, triage, generate_reply, dispatch)"
    )
    organization_id: str = Field(description="Mandatory tenant isolation UUID (R23.6)")
    message_id: str = Field(default="", description="Target normalized message UUID")
    thread_id: str = Field(default="", description="Target conversation thread UUID")
    mailbox_id: str | None = Field(default=None, description="Target mailbox UUID")
    trace_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        description="Distributed OpenTelemetry trace context identifier (R21.2)",
    )
    attempt: int = Field(default=0, ge=0, description="Delivery attempt count for retry ladder")
    classification: dict[str, Any] = Field(
        default_factory=dict,
        description="Classification snapshot so worker never re-classifies (R7.3)",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary job payload/signal metadata",
    )
    enqueued_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="UTC timestamp when job was enqueued",
    )

    def to_message(self, headers: dict[str, Any] | None = None) -> aio_pika.Message:
        """Serialize envelope into a persistent aio-pika AMQP Message (R3.1).

        Parameters
        ----------
        headers : dict[str, Any] | None
            Optional metadata headers to attach to the AMQP properties.

        Returns
        -------
        aio_pika.Message
            Durable message with delivery_mode=PERSISTENT and application/json content type.
        """
        payload_bytes = self.model_dump_json().encode("utf-8")
        msg_headers: dict[str, Any] = headers.copy() if headers else {}
        msg_headers.setdefault("trace_id", self.trace_id)
        msg_headers.setdefault("organization_id", self.organization_id)
        msg_headers.setdefault("job_id", self.job_id)
        if self.mailbox_id:
            msg_headers.setdefault("mailbox_id", self.mailbox_id)

        # Inject OpenTelemetry W3C trace context (R21.1)
        try:
            from packages.observability.tracing import inject_trace_context

            msg_headers = inject_trace_context(msg_headers)
        except Exception:
            pass

        return aio_pika.Message(
            body=payload_bytes,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            content_type="application/json",
            content_encoding="utf-8",
            message_id=self.job_id,
            correlation_id=self.trace_id,
            headers=msg_headers,
            timestamp=self.enqueued_at,
        )

    @classmethod
    def from_message(cls, message: AbstractIncomingMessage) -> "JobEnvelope":
        """Deserialize an incoming AMQP Message into a validated JobEnvelope.

        Parameters
        ----------
        message : AbstractIncomingMessage
            The received AMQP message.

        Returns
        -------
        JobEnvelope
            Validated envelope model instance.
        """
        raw_json = json.loads(message.body.decode("utf-8"))
        return cls.model_validate(raw_json)
