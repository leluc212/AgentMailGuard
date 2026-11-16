"""Run options on the job (task 9.2, R25.3, R18.1, R19.1).

A demo job carries ``run_options = {model_profile, guard_mode}``. It is stored on the job and
copied into every envelope of that job, so the triage hop, a retry and an operator replay all
keep it. A job without run options behaves as before R25 (the field stays None).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from packages.adapters.fake import FakeProviderAdapter
from packages.broker.envelope import JobEnvelope
from packages.broker.retry import handle_job_transient_failure
from packages.broker.routing import prepare_route_envelope
from packages.core.settings import BrokerSettings, RetryLadderSettings
from packages.core.storage import FakeObjectStorageClient
from packages.db.checkpoint import InMemoryCheckpointStore
from packages.db.job import InMemoryJobStore
from packages.db.mailbox import InMemoryMailboxStore
from packages.db.message import InMemoryMessageStore
from packages.domain.entities import Classification, Job, Mailbox
from packages.domain.state_machine import JobState
from services.api.main import create_app
from services.mail_connector.orchestrator import SyncOrchestrator

DEMO = {"model_profile": "fake", "guard_mode": "C3"}


def _envelope(run_options: dict[str, Any] | None = DEMO) -> JobEnvelope:
    return JobEnvelope(
        job_id=str(uuid4()),
        idempotency_key=f"k-{uuid4()}",
        job_type="triage_email",
        organization_id=str(uuid4()),
        message_id=str(uuid4()),
        run_options=run_options,
    )


def test_an_envelope_without_run_options_is_unchanged() -> None:
    envelope = JobEnvelope(idempotency_key="k", job_type="triage_email", organization_id="o")

    assert envelope.run_options is None
    assert JobEnvelope.model_validate_json(envelope.model_dump_json()).run_options is None


def test_run_options_survive_the_wire() -> None:
    message = _envelope().to_message()
    incoming = MagicMock()
    incoming.body = message.body

    assert JobEnvelope.from_message(incoming).run_options == DEMO


def test_the_route_to_the_ai_worker_keeps_run_options() -> None:
    _, routed = prepare_route_envelope(_envelope(), Classification(category="billing"))

    assert routed.job_type == "generate_reply"
    assert routed.run_options == DEMO


async def test_a_retry_keeps_run_options() -> None:
    publisher = MagicMock()
    publisher.settings = BrokerSettings()
    publisher.publish_to_retry = AsyncMock()

    retried = await handle_job_transient_failure(
        envelope=_envelope(),
        exception=TimeoutError("model timed out"),
        publisher=publisher,
        retry_settings=RetryLadderSettings(),
        origin_exchange="email.route",
        origin_routing_key="email.billing.normal",
        queue_name="email.billing.normal",
    )

    assert retried.attempt == 1
    assert retried.run_options == DEMO


async def test_the_job_store_keeps_run_options() -> None:
    store = InMemoryJobStore()
    org = uuid4()
    job, _ = await store.create_job(
        Job(organization_id=org, idempotency_key="k", run_options=DEMO)
    )
    plain, _ = await store.create_job(Job(organization_id=org, idempotency_key="k2"))

    stored = await store.get_job(org, job.id)
    assert stored is not None and stored.run_options == DEMO
    assert plain.run_options is None


class _Publisher:
    def __init__(self) -> None:
        self.published: list[JobEnvelope] = []

    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        envelope: JobEnvelope,
        headers: dict[str, Any] | None = None,
    ) -> None:
        self.published.append(envelope)


def _mailbox() -> Mailbox:
    return Mailbox(
        id=uuid4(),
        organization_id=uuid4(),
        provider="fake",
        address="demo@example.com",
        display_name="Demo",
        status="active",
    )


@pytest.mark.parametrize("run_options", [DEMO, None])
async def test_a_sync_stores_run_options_on_the_job_and_its_envelope(
    run_options: dict[str, Any] | None,
) -> None:
    mailbox = _mailbox()
    adapter = FakeProviderAdapter()
    adapter.seed_message(provider_message_id="m-1", raw_payload=b"From: a@b.com\n\nHi")
    jobs = InMemoryJobStore()
    publisher = _Publisher()
    orchestrator = SyncOrchestrator(
        checkpoint_store=InMemoryCheckpointStore(),
        storage_client=FakeObjectStorageClient(),
        publisher=publisher,
        adapter_resolver=lambda _: adapter,
        mailbox_store=InMemoryMailboxStore([mailbox]),
        job_store=jobs,
    )

    await orchestrator.sync_mailbox(mailbox, run_options=run_options)

    (envelope,) = publisher.published
    assert envelope.run_options == run_options
    job = await jobs.get_job(mailbox.organization_id, UUID(envelope.job_id))
    assert job is not None and job.run_options == run_options


@pytest.fixture
def publisher() -> MagicMock:
    pub = MagicMock()
    pub.publish = AsyncMock()
    pub.settings = BrokerSettings()
    pub.broker_settings = pub.settings
    return pub


@pytest.fixture
def app(publisher: MagicMock) -> FastAPI:
    app = create_app(lifespan_enabled=False)
    app.state.job_store = InMemoryJobStore()
    app.state.message_store = InMemoryMessageStore()
    app.state.publisher = publisher
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        yield ac


@pytest.mark.parametrize("run_options", [DEMO, None])
async def test_an_operator_replay_keeps_run_options(
    app: FastAPI,
    client: AsyncClient,
    publisher: MagicMock,
    run_options: dict[str, Any] | None,
) -> None:
    org, job_id = uuid4(), uuid4()
    await app.state.job_store.create_job(
        Job(
            id=job_id,
            organization_id=org,
            message_id=uuid4(),
            job_type="generate_reply",
            state=JobState.DEAD_LETTER.value,
            attempt=5,
            queue_name="email.billing.normal",
            idempotency_key=f"idem-{job_id}",
            run_options=run_options,
        )
    )

    response = await client.post(
        f"/v1/jobs/{job_id}/replay",
        headers={"X-Organization-ID": str(org)},
        json={"reset_attempts": True, "reason": "demo S10"},
    )

    assert response.status_code == status.HTTP_200_OK
    envelope = publisher.publish.await_args.kwargs["envelope"]
    assert envelope.run_options == run_options
