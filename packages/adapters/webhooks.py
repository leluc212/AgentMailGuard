"""Provider webhook receivers and validation handshakes.

Requirements:
- R2.1: Expose provider webhook endpoints accepting notifications and completing handshakes.
- R2.2: Acknowledge within 5s with NO synchronous provider fetch inside the request.
- R2.3: Treat notification as a signal only; never trust payload as authoritative state.
- R3.1: Enqueue sync_mailbox job to RabbitMQ (mail.sync queue).
- GEMINI.md §4: Provider names ('gmail', 'graph') appear only inside packages/adapters/.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse

from packages.adapters.gmail import parse_pubsub_notification
from packages.adapters.graph import parse_graph_notification
from packages.broker.envelope import JobEnvelope
from packages.core.settings import BrokerSettings
from packages.observability.context import bind_log_context
from packages.observability.tracing import get_current_trace_id, trace_span

logger = logging.getLogger("adapters.webhooks")

webhook_router = APIRouter(
    prefix="/v1/webhooks",
    tags=["webhooks"],
)


def get_job_publisher(request: Request) -> Any:
    """Retrieve message publisher from FastAPI app.state if available."""
    return getattr(request.app.state, "publisher", None)


PublisherDep = Annotated[Any, Depends(get_job_publisher)]


async def resolve_mailbox_info(
    request: Request,
    mailbox_id: str | None = None,
    address: str | None = None,
) -> tuple[str, str]:
    """Resolve (mailbox_id, organization_id) via DB lookup if available, or fallback."""
    db_pool = getattr(request.app.state, "db_pool", None)
    if db_pool is not None:
        try:
            async with db_pool.acquire() as conn:
                if mailbox_id:
                    row = await conn.fetchrow(
                        "SELECT id, organization_id FROM mailbox WHERE id = $1",
                        mailbox_id,
                    )
                    if row:
                        return str(row["id"]), str(row["organization_id"])
                elif address:
                    row = await conn.fetchrow(
                        "SELECT id, organization_id FROM mailbox WHERE address = $1",
                        address,
                    )
                    if row:
                        return str(row["id"]), str(row["organization_id"])
        except Exception as exc:
            logger.warning("DB lookup for mailbox (%s, %s) failed: %s", mailbox_id, address, exc)

    fallback_org = (
        request.headers.get("X-Organization-ID")
        or request.query_params.get("organization_id")
        or "00000000-0000-0000-0000-000000000000"
    )
    resolved_mbx = mailbox_id or address or f"unresolved-{uuid.uuid4().hex[:8]}"
    return resolved_mbx, fallback_org


async def enqueue_sync_job(
    publisher: Any,
    organization_id: str,
    mailbox_id: str,
    provider: str,
    signal_payload: dict[str, Any],
    trace_id: str | None = None,
    broker_settings: BrokerSettings | None = None,
) -> str:
    """Enqueue a sync_mailbox job to the mail ingest exchange (R2.1, R2.2, R3.1)."""
    b_cfg = broker_settings or BrokerSettings()
    job_trace = trace_id or get_current_trace_id() or uuid.uuid4().hex
    job = JobEnvelope(
        idempotency_key=f"{organization_id}:{mailbox_id}:sync:{uuid.uuid4().hex[:8]}",
        job_type="sync_mailbox",
        organization_id=organization_id,
        mailbox_id=mailbox_id,
        trace_id=job_trace,
        payload={
            "provider": provider,
            **signal_payload,
        },
    )

    if publisher is not None:
        await publisher.publish(
            exchange_name=b_cfg.exchange_mail_ingest,
            routing_key=b_cfg.queue_mail_sync,
            envelope=job,
        )
        logger.info(
            "Enqueued sync_mailbox job %s for mailbox %s (provider=%s)",
            job.job_id,
            mailbox_id,
            provider,
        )
    else:
        logger.warning(
            "Publisher not attached; sync_mailbox job %s simulated for mailbox %s",
            job.job_id,
            mailbox_id,
        )

    return job.job_id


# ---------------------------------------------------------------------------
# Microsoft Graph Webhook Handshake & Notification Endpoints (R2.1, R2.2, R2.3)
# ---------------------------------------------------------------------------


@webhook_router.get("/graph", summary="Microsoft Graph Webhook Validation Handshake")
@webhook_router.get("/graph/{mailbox_id}", summary="Microsoft Graph Mailbox Webhook Validation")
async def graph_validation_get(
    validation_token: Annotated[
        str | None, Query(alias="validationToken", description="Graph validation token")
    ] = None,
) -> Response:
    """Handle GET validation handshake from Microsoft Graph."""
    if validation_token:
        return Response(
            content=validation_token,
            status_code=status.HTTP_200_OK,
            media_type="text/plain; charset=utf-8",
        )
    return Response(content="OK", status_code=status.HTTP_200_OK, media_type="text/plain")


@webhook_router.post("/graph", summary="Microsoft Graph Webhook Ingestion")
@webhook_router.post("/graph/{mailbox_id}", summary="Microsoft Graph Mailbox Webhook Ingestion")
async def graph_webhook_post(
    request: Request,
    mailbox_id: str | None = None,
    validation_token: Annotated[
        str | None, Query(alias="validationToken", description="Graph validation token")
    ] = None,
    publisher: PublisherDep = None,
) -> Response:
    """Handle Microsoft Graph change notifications and validation handshake (R2.1, R2.2, R2.3)."""
    # 1. Complete validation handshake if validationToken is present in query (R2.1)
    if validation_token:
        logger.info("Completing Microsoft Graph validation handshake")
        return Response(
            content=validation_token,
            status_code=status.HTTP_200_OK,
            media_type="text/plain; charset=utf-8",
        )

    # 2. Parse change notification body as signal only (R2.3)
    raw_body = await request.body()
    if not raw_body or raw_body.strip() == b"{}":
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ok"})

    try:
        notifications = parse_graph_notification(raw_body)
    except Exception as err:
        logger.warning("Failed to parse Microsoft Graph notification: %s", err)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "Malformed Graph notification payload", "detail": str(err)},
        )

    enqueued_job_ids: list[str] = []

    with trace_span("webhook.graph.receive", attributes={"provider": "graph"}):
        trace_id = get_current_trace_id() or uuid.uuid4().hex

        for notif in notifications:
            # Extract mailbox hint from client_state if formatted as 'secret-{mailbox_id}'
            client_hint: str | None = None
            if notif.client_state and notif.client_state.startswith("secret-"):
                client_hint = notif.client_state.removeprefix("secret-")

            target_mbx = mailbox_id or client_hint or notif.resource
            resolved_mbx, org_id = await resolve_mailbox_info(
                request,
                mailbox_id=target_mbx,
            )

            bind_log_context(organization_id=org_id, trace_id=trace_id)

            # Enqueue sync_mailbox job without synchronous provider fetch (R2.2, R2.3)
            job_id = await enqueue_sync_job(
                publisher=publisher,
                organization_id=org_id,
                mailbox_id=resolved_mbx,
                provider="graph",
                signal_payload={
                    "subscription_id": notif.subscription_id,
                    "change_type": notif.change_type,
                    "resource": notif.resource,
                    "resource_id": notif.resource_id,
                },
                trace_id=trace_id,
            )
            enqueued_job_ids.append(job_id)

    # Acknowledge within 5s with 202 Accepted (R2.2)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status": "accepted",
            "enqueued_count": len(enqueued_job_ids),
            "job_ids": enqueued_job_ids,
        },
    )


# ---------------------------------------------------------------------------
# Gmail / Google Cloud Pub/Sub Push Webhook Endpoints (R2.1, R2.2, R2.3)
# ---------------------------------------------------------------------------


@webhook_router.get("/gmail", summary="Gmail Pub/Sub Webhook Verification Probe")
@webhook_router.get("/gmail/{mailbox_id}", summary="Gmail Mailbox Pub/Sub Verification Probe")
async def gmail_validation_get(
    challenge: Annotated[str | None, Query(description="Optional challenge token")] = None,
    validation_token: Annotated[
        str | None, Query(alias="validationToken", description="Optional validation token")
    ] = None,
    token: Annotated[str | None, Query(description="Optional verification token")] = None,
) -> Response:
    """Handle verification probes and challenge exchanges for Gmail / Pub/Sub."""
    probe_token = challenge or validation_token or token
    if probe_token:
        return Response(
            content=probe_token,
            status_code=status.HTTP_200_OK,
            media_type="text/plain; charset=utf-8",
        )
    return Response(content="OK", status_code=status.HTTP_200_OK, media_type="text/plain")


@webhook_router.post("/gmail", summary="Gmail Pub/Sub Push Notification Ingestion")
@webhook_router.post("/gmail/{mailbox_id}", summary="Gmail Mailbox Pub/Sub Push Ingestion")
async def gmail_webhook_post(
    request: Request,
    mailbox_id: str | None = None,
    challenge: Annotated[str | None, Query(description="Optional challenge token")] = None,
    validation_token: Annotated[
        str | None, Query(alias="validationToken", description="Optional validation token")
    ] = None,
    token: Annotated[str | None, Query(description="Optional verification token")] = None,
    publisher: PublisherDep = None,
) -> Response:
    """Handle Gmail Cloud Pub/Sub push notifications and verification handshakes."""
    # 1. Complete validation handshake if challenge/token is present (R2.1)
    probe_token = challenge or validation_token or token
    if probe_token:
        logger.info("Completing Gmail/PubSub verification handshake")
        return Response(
            content=probe_token,
            status_code=status.HTTP_200_OK,
            media_type="text/plain; charset=utf-8",
        )

    # 2. Parse Pub/Sub envelope body as signal only (R2.3)
    raw_body = await request.body()
    if not raw_body or raw_body.strip() == b"{}":
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ok"})

    try:
        notif = parse_pubsub_notification(raw_body)
    except Exception as err:
        logger.warning("Failed to parse Gmail Pub/Sub notification: %s", err)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "Malformed Pub/Sub notification payload", "detail": str(err)},
        )

    with trace_span("webhook.gmail.receive", attributes={"provider": "gmail"}):
        trace_id = get_current_trace_id() or uuid.uuid4().hex

        resolved_mbx, org_id = await resolve_mailbox_info(
            request,
            mailbox_id=mailbox_id,
            address=notif.email_address,
        )

        bind_log_context(organization_id=org_id, trace_id=trace_id)

        # Enqueue sync_mailbox job without synchronous provider fetch (R2.2, R2.3)
        job_id = await enqueue_sync_job(
            publisher=publisher,
            organization_id=org_id,
            mailbox_id=resolved_mbx,
            provider="gmail",
            signal_payload={
                "email_address": notif.email_address,
                "history_id": notif.history_id,
            },
            trace_id=trace_id,
        )

    # Acknowledge Pub/Sub push notification with 200 OK within 5s (R2.2)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "acknowledged",
            "job_id": job_id,
            "mailbox_id": resolved_mbx,
        },
    )
