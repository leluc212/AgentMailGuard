"""Demo console ingest door (ADR-0018 decision 3, design.md §5.9, R25.1, R25.3, R25.4, R23.6).

Mounted only when ``DEMO__ENABLED=true``; otherwise every ``/v1/demo`` path answers 404.

``POST /v1/demo/messages`` never writes a message or job row: it builds the email as MIME and
publishes a ``mail.demo.inject`` envelope; the mail-connector ingests it through
``SyncOrchestrator.sync_mailbox`` like a fetched email (R25.1). The request id names the email's
provider message id, so ``GET /v1/demo/requests/{id}`` finds the job by the idempotency key the
connector derives for it; no table of requests is needed.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status

from packages.adapters.demo import (
    DEMO_INJECT_JOB_TYPE,
    DemoInboundMessage,
    build_demo_mime,
    demo_message_id,
    find_demo_mailbox,
    inject_payload,
)
from packages.broker.envelope import JobEnvelope
from packages.core.idempotency import derive_idempotency_key
from packages.domain.entities import Mailbox
from packages.llm.live_models import LiveModelCatalog, load_live_models
from services.api.demo_catalog import Scenario, ScenarioCatalog
from services.api.dependencies import (
    JobStoreDep,
    MailboxStoreDep,
    PublisherDep,
    get_organization_id,
)
from services.api.schemas.demo import (
    DemoCapabilities,
    DemoMessageAccepted,
    DemoMessageRequest,
    DemoRequestStatus,
    DemoRunOptions,
    DemoThreadMessage,
    LiveModelOption,
    ScenarioRunAccepted,
    ScenarioRunRequest,
)

logger = logging.getLogger(__name__)

demo_router = APIRouter(
    prefix="/v1/demo",
    tags=["demo"],
    dependencies=[Depends(get_organization_id)],
)

OrgIdDep = Annotated[UUID, Depends(get_organization_id)]


def _settings(request: Request) -> Any:
    return request.app.state.settings


def llm_provider_is_live(settings: Any) -> bool:
    """The LLM__* provider is a live one (anything but the offline fake, R25.4)."""
    return str(settings.llm.provider).strip().lower() != "fake"


def live_catalog(request: Request) -> LiveModelCatalog:
    """The live model catalog (ADR-0019), read once per process with its keys' presence."""
    catalog: LiveModelCatalog | None = getattr(request.app.state, "live_models", None)
    if catalog is None:
        catalog = load_live_models(_settings(request).demo.live_models_path)
        request.app.state.live_models = catalog
    return catalog


def live_model_available(settings: Any, catalog: LiveModelCatalog | None = None) -> bool:
    """Some live model can run: the LLM__* provider or a catalog model with its key."""
    if llm_provider_is_live(settings):
        return True
    return catalog is not None and any(catalog.available(m.name) for m in catalog.models)


def request_message_id(demo_request_id: UUID) -> str:
    """Provider message id of a request's own email (its thread messages get a suffix)."""
    return f"demo-{demo_request_id}"


def _unavailable(error: str, code: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"error": error, "code": code}
    )


async def _demo_mailbox(store: Any, org_id: UUID) -> Mailbox:
    mailbox = await find_demo_mailbox(store, org_id)
    if mailbox is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": f"Organization {org_id} has no demo mailbox; run `make seed` first.",
                "code": "DEMO_MAILBOX_MISSING",
            },
        )
    return mailbox


def build_messages(
    body: DemoMessageRequest, *, demo_request_id: UUID, recipient: str, now: datetime
) -> list[DemoInboundMessage]:
    """The request's emails as MIME, oldest first, chained by In-Reply-To and References."""
    thread_id = f"demo-thread-{demo_request_id}"
    entries: list[DemoThreadMessage | DemoMessageRequest] = [*body.thread, body]
    messages: list[DemoInboundMessage] = []
    rfc_ids: list[str] = []
    for index, entry in enumerate(entries):
        last = index == len(entries) - 1
        provider_id = (
            request_message_id(demo_request_id) if last else f"demo-{demo_request_id}-{index}"
        )
        rfc_id = demo_message_id(provider_id)
        received_at = now - timedelta(minutes=len(entries) - 1 - index)
        raw = build_demo_mime(
            sender_email=entry.sender_email,
            sender_name=entry.sender_name,
            recipient=recipient,
            subject=entry.subject,
            body=entry.body,
            message_id=rfc_id,
            date=received_at,
            in_reply_to=rfc_ids[-1] if rfc_ids else None,
            references=tuple(rfc_ids),
        )
        messages.append(
            DemoInboundMessage(
                provider_message_id=provider_id,
                provider_thread_id=thread_id,
                raw_mime=raw,
                received_at=received_at,
            )
        )
        rfc_ids.append(rfc_id)
    return messages


@demo_router.get(
    "/capabilities",
    summary="Demo capabilities",
    response_model=DemoCapabilities,
)
async def get_capabilities(
    request: Request, org_id: OrgIdDep, mailbox_store: MailboxStoreDep
) -> DemoCapabilities:
    """Whether the live model and the guard are available, and the tenant's demo mailbox."""
    settings = _settings(request)
    mailbox = await find_demo_mailbox(mailbox_store, org_id)
    catalog = live_catalog(request)
    return DemoCapabilities(
        live_model_available=live_model_available(settings, catalog),
        live_model=settings.llm.fast_model if llm_provider_is_live(settings) else None,
        live_models=[
            LiveModelOption(
                name=m.name, label=m.label, model=m.model, available=catalog.available(m.name)
            )
            for m in catalog.models
        ],
        guard_available=bool(settings.guard.enabled),
        # The classifier is mounted into the api container as into the ai-worker (compose)
        l1_classifier_loaded=Path(settings.guard.l1_model_path).is_file(),
        demo_mailbox_id=UUID(str(mailbox.id)) if mailbox else None,
    )


@demo_router.post(
    "/messages",
    summary="Inject a demo email",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DemoMessageAccepted,
)
async def post_demo_message(
    request: Request,
    body: DemoMessageRequest,
    org_id: OrgIdDep,
    mailbox_store: MailboxStoreDep,
    publisher: PublisherDep,
) -> DemoMessageAccepted:
    """Queue one demo email for the mail-connector; nothing is written here (R25.1).

    Raises:
        HTTPException: 422 for `live` without a live provider (R25.4), 409 without a demo
            mailbox, 503 when the broker is unavailable. Nothing is published in any of them.
    """
    settings = _settings(request)
    _check_live(settings, body.run_options, live_catalog(request))
    mailbox = await _demo_mailbox(mailbox_store, org_id)
    if publisher is None:
        raise _unavailable("Message broker publisher is not available.", "PUBLISHER_UNAVAILABLE")
    return await _queue(settings, publisher, org_id, mailbox, body)


def _check_live(
    settings: Any, run_options: DemoRunOptions, catalog: LiveModelCatalog | None = None
) -> None:
    """R25.4: `live` without a matching live provider is refused before anything is published.

    A named ``live_model`` must be a catalog model whose key is set; without a name the LLM__*
    provider runs, or the catalog's only available model (what the workers pick, ADR-0019).
    """
    if run_options.model_profile != "live":
        return
    name = run_options.live_model
    if name:
        if catalog is None or catalog.get(name) is None:
            raise _live_refused(f"Unknown live model '{name}' (config/live_models.yaml).")
        if not catalog.available(name):
            env = catalog.get(name).api_key_env  # type: ignore[union-attr]
            raise _live_refused(f"Live model '{name}' has no key: set {env} in .env.")
        return
    if llm_provider_is_live(settings):
        return
    available = [m.name for m in catalog.models if catalog.available(m.name)] if catalog else []
    if len(available) != 1:
        raise _live_refused(
            "model_profile=live needs a live model: pick one of config/live_models.yaml "
            "(live_model) or set LLM__PROVIDER."
        )


def _live_refused(error: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"error": error, "code": "LIVE_MODEL_UNAVAILABLE"},
    )


async def _queue(
    settings: Any, publisher: Any, org_id: UUID, mailbox: Mailbox, body: DemoMessageRequest
) -> DemoMessageAccepted:
    """Build the request's emails and publish one ``mail.demo.inject`` envelope."""
    demo_request_id = uuid4()
    messages = build_messages(
        body, demo_request_id=demo_request_id, recipient=mailbox.address, now=datetime.now(UTC)
    )
    envelope = JobEnvelope(
        job_id=str(demo_request_id),
        idempotency_key=f"demo-inject:{org_id}:{demo_request_id}",
        job_type=DEMO_INJECT_JOB_TYPE,
        organization_id=str(org_id),
        mailbox_id=str(mailbox.id),
        payload=inject_payload(mailbox.id, messages),
        # no `live_model: null`: a run that names none keeps the run options of before ADR-0019
        run_options=body.run_options.model_dump(exclude_none=True),
    )
    broker = settings.broker
    try:
        await publisher.publish(
            exchange_name=broker.exchange_mail_ingest,
            routing_key=broker.queue_demo_inject,
            envelope=envelope,
        )
    except Exception as err:
        logger.error("Demo request %s could not be queued: %s", demo_request_id, err)
        raise _unavailable(
            "The demo email could not be queued; try again.", "PUBLISH_FAILED"
        ) from err

    logger.info(
        "Demo request %s queued: %d message(s) for mailbox %s (run_options=%s)",
        demo_request_id,
        len(messages),
        mailbox.id,
        envelope.run_options,
    )
    return DemoMessageAccepted(
        demo_request_id=demo_request_id, messages=len(messages), run_options=body.run_options
    )


def _catalog(request: Request) -> ScenarioCatalog:
    catalog: ScenarioCatalog = request.app.state.demo_catalog
    return catalog


@demo_router.get(
    "/scenarios",
    summary="Demo scenario catalog",
    response_model=list[Scenario],
)
async def list_scenarios(request: Request) -> list[Scenario]:
    """S01-S14 and G01-G06 with their emails, run options and expected outcomes (R25.2)."""
    return _catalog(request).scenarios


@demo_router.post(
    "/scenarios/{scenario_id}/run",
    summary="Run a demo scenario",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ScenarioRunAccepted,
)
async def run_scenario(
    request: Request,
    scenario_id: str,
    org_id: OrgIdDep,
    mailbox_store: MailboxStoreDep,
    publisher: PublisherDep,
    body: ScenarioRunRequest | None = None,
) -> ScenarioRunAccepted:
    """Queue the scenario's email once per run: twice for a comparison (C0, C3), as independent
    jobs in two threads (R25.8), N times for a burst.

    Raises:
        HTTPException: 404 for an unknown id, 422 for a scenario that sends no email (S14) or for
            `live` without a live provider, plus the errors of POST /v1/demo/messages.
    """
    scenario = _catalog(request).get(scenario_id)
    if scenario is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": f"Unknown scenario '{scenario_id}'.", "code": "SCENARIO_NOT_FOUND"},
        )
    if not scenario.runnable or scenario.email is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": f"Scenario {scenario.id} sends no email; the console drives it.",
                "code": "SCENARIO_NOT_RUNNABLE",
            },
        )
    settings = _settings(request)
    profile = body.model_profile if body and body.model_profile else None
    if scenario.requires_live:
        profile = "live"  # decision C13: the model switch cannot make G06 fake
    live_model = body.live_model if body else None
    runs = [
        run.model_copy(update={"model_profile": profile}) if profile else run
        for run in scenario.runs()
    ]
    if live_model:  # the console's model picker (ADR-0019); only a live run reads it
        runs = [run.model_copy(update={"live_model": live_model}) for run in runs]
    for run in runs:
        _check_live(settings, run, live_catalog(request))
    mailbox = await _demo_mailbox(mailbox_store, org_id)
    if publisher is None:
        raise _unavailable("Message broker publisher is not available.", "PUBLISHER_UNAVAILABLE")

    accepted = []
    for run in runs:
        message = DemoMessageRequest(
            **scenario.email.model_dump(), thread=scenario.thread, run_options=run
        )
        accepted.append(await _queue(settings, publisher, org_id, mailbox, message))
    return ScenarioRunAccepted(scenario_id=scenario.id, requests=accepted)


@demo_router.get(
    "/requests/{demo_request_id}",
    summary="Demo request status",
    response_model=DemoRequestStatus,
)
async def get_demo_request(
    demo_request_id: UUID,
    org_id: OrgIdDep,
    mailbox_store: MailboxStoreDep,
    job_store: JobStoreDep,
) -> DemoRequestStatus:
    """`pending` until the connector created the job of the request's email, then its ids."""
    mailbox = await _demo_mailbox(mailbox_store, org_id)
    key = derive_idempotency_key(
        organization_id=org_id,
        mailbox_id=mailbox.id,
        provider_message_id=request_message_id(demo_request_id),
        operation_type="normalize",
    )
    job = await job_store.get_job_by_idempotency_key(org_id, key)
    if job is None:
        return DemoRequestStatus(demo_request_id=demo_request_id, status="pending")
    return DemoRequestStatus(
        demo_request_id=demo_request_id,
        status="ingested",
        job_id=UUID(str(job.id)),
        message_id=UUID(str(job.message_id)) if job.message_id else None,
        job_state=job.state,
        run_options=DemoRunOptions(**job.run_options) if job.run_options else None,
    )
