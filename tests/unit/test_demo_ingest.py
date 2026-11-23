"""Demo ingest door (task 9.3, R25.1, R25.4, R23.1, R23.6, R3.1).

The API only publishes a ``mail.demo.inject`` envelope; the mail-connector's consumer hands the
email to ``SyncOrchestrator.sync_mailbox`` with the demo adapter, which creates the job (with its
run options) and publishes to ``email.normalize``. Fixtures seed three tenants, each with a demo
mailbox, so a lookup that ignored the tenant would pick the wrong mailbox.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email import message_from_bytes, policy
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from packages.adapters.demo import (
    DEMO_INJECT_JOB_TYPE,
    DemoMailAdapter,
    build_demo_mime,
    demo_seed_mailbox,
    messages_from_payload,
)
from packages.adapters.registry import get_adapter_for_mailbox
from packages.broker.consumer import FatalError
from packages.broker.envelope import JobEnvelope
from packages.core.idempotency import derive_idempotency_key
from packages.core.settings import (
    APISettings,
    BrokerSettings,
    DemoSettings,
    LLMTiersSettings,
    MailConnectorSettings,
)
from packages.core.storage import FakeObjectStorageClient
from packages.db.checkpoint import InMemoryCheckpointStore
from packages.db.job import InMemoryJobStore
from packages.db.mailbox import InMemoryMailboxStore
from packages.domain.entities import Job, Mailbox, OutboundReply
from services.api.main import create_app
from services.mail_connector.demo_consumer import DemoInjectConsumer
from services.mail_connector.main import build_components
from services.mail_connector.orchestrator import SyncOrchestrator
from tests.stubs.worker_resources import fake_worker_resources

ORGS = [uuid4(), uuid4(), uuid4()]


def _demo_mailbox(org: UUID, name: str) -> Mailbox:
    row = demo_seed_mailbox(
        mailbox_id=uuid4(), organization_id=org, address=f"demo@{name}.com", display_name=name
    )
    return Mailbox(**row)


def _real_mailbox(org: UUID) -> Mailbox:
    return Mailbox(
        id=uuid4(), organization_id=org, provider="imap", address="ops@x.com", status="active"
    )


@pytest.fixture
def mailboxes() -> list[Mailbox]:
    names = ("acme", "beta", "gamma")
    boxes = [_demo_mailbox(org, name) for org, name in zip(ORGS, names, strict=True)]
    return [*boxes, *(_real_mailbox(org) for org in ORGS)]


@pytest.fixture
def publisher() -> MagicMock:
    pub = MagicMock()
    pub.publish = AsyncMock()
    pub.settings = BrokerSettings()
    return pub


def _app(
    *, enabled: bool, mailboxes: list[Mailbox], publisher: Any, llm_provider: str = "fake"
) -> FastAPI:
    settings = APISettings(
        _env_file=None,
        demo=DemoSettings(enabled=enabled),
        llm=LLMTiersSettings(provider=llm_provider),
    )
    app = create_app(settings=settings, lifespan_enabled=False)
    app.state.mailbox_store = InMemoryMailboxStore(mailboxes)
    app.state.job_store = InMemoryJobStore()
    app.state.publisher = publisher
    return app


@pytest.fixture
def app(mailboxes: list[Mailbox], publisher: MagicMock) -> FastAPI:
    return _app(enabled=True, mailboxes=mailboxes, publisher=publisher)


async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        yield ac


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async for ac in _client(app):
        yield ac


def _headers(org: UUID) -> dict[str, str]:
    return {"X-Organization-ID": str(org)}


EMAIL = {
    "sender_email": "alice@example.com",
    "sender_name": "Alice",
    "subject": "Invoice INV-2026-001",
    "body": "What are the payment terms?",
    "run_options": {"model_profile": "fake", "guard_mode": "C3"},
}


# --- the API door ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/demo/capabilities"),
        ("POST", "/v1/demo/messages"),
        ("GET", f"/v1/demo/requests/{uuid4()}"),
    ],
)
async def test_every_demo_path_is_404_when_the_demo_is_disabled(
    mailboxes: list[Mailbox], publisher: MagicMock, method: str, path: str
) -> None:
    app = _app(enabled=False, mailboxes=mailboxes, publisher=publisher)
    async for client in _client(app):
        response = await client.request(method, path, headers=_headers(ORGS[0]), json=EMAIL)

    assert response.status_code == status.HTTP_404_NOT_FOUND
    publisher.publish.assert_not_awaited()


async def test_a_demo_email_is_only_published_to_the_connector(
    app: FastAPI, client: AsyncClient, publisher: MagicMock, mailboxes: list[Mailbox]
) -> None:
    response = await client.post("/v1/demo/messages", headers=_headers(ORGS[1]), json=EMAIL)

    assert response.status_code == status.HTTP_202_ACCEPTED
    body = response.json()
    assert body["messages"] == 1
    kwargs = publisher.publish.await_args.kwargs
    assert kwargs["exchange_name"] == "mail.ingest"
    assert kwargs["routing_key"] == "mail.demo.inject"
    envelope: JobEnvelope = kwargs["envelope"]
    assert envelope.job_type == DEMO_INJECT_JOB_TYPE
    assert envelope.organization_id == str(ORGS[1])
    assert envelope.mailbox_id == str(mailboxes[1].id)  # beta's demo mailbox, not another's
    assert envelope.run_options == {"model_profile": "fake", "guard_mode": "C3"}
    (message,) = messages_from_payload(envelope.payload)
    assert message.provider_message_id == f"demo-{body['demo_request_id']}"
    parsed = message_from_bytes(message.raw_mime, policy=policy.default)
    assert parsed["To"] == "demo@beta.com"
    assert parsed.get_content().strip() == EMAIL["body"]
    # R25.1: the API wrote no job row
    assert app.state.job_store._jobs == {}


async def test_thread_messages_come_first_and_are_chained(
    client: AsyncClient, publisher: MagicMock
) -> None:
    earlier = {"sender_email": "alice@example.com", "subject": "Order", "body": "First"}
    response = await client.post(
        "/v1/demo/messages", headers=_headers(ORGS[0]), json={**EMAIL, "thread": [earlier]}
    )

    assert response.json()["messages"] == 2
    first, last = messages_from_payload(publisher.publish.await_args.kwargs["envelope"].payload)
    assert first.provider_thread_id == last.provider_thread_id
    assert first.received_at < last.received_at
    first_id = message_from_bytes(first.raw_mime, policy=policy.default)["Message-ID"]
    assert message_from_bytes(last.raw_mime, policy=policy.default)["In-Reply-To"] == first_id


async def test_live_without_a_live_provider_is_422_and_publishes_nothing(
    client: AsyncClient, publisher: MagicMock
) -> None:
    live = {**EMAIL, "run_options": {"model_profile": "live", "guard_mode": "C0"}}

    response = await client.post("/v1/demo/messages", headers=_headers(ORGS[0]), json=live)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["code"] == "LIVE_MODEL_UNAVAILABLE"
    publisher.publish.assert_not_awaited()


async def test_live_with_a_live_provider_is_accepted(
    mailboxes: list[Mailbox], publisher: MagicMock
) -> None:
    app = _app(enabled=True, mailboxes=mailboxes, publisher=publisher, llm_provider="openai")
    live = {**EMAIL, "run_options": {"model_profile": "live", "guard_mode": "C0"}}
    async for client in _client(app):
        response = await client.post("/v1/demo/messages", headers=_headers(ORGS[0]), json=live)
        caps = await client.get("/v1/demo/capabilities", headers=_headers(ORGS[0]))

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert caps.json()["live_model_available"] is True


async def test_a_catalog_live_model_runs_only_with_its_key(
    mailboxes: list[Mailbox], publisher: MagicMock
) -> None:
    from packages.llm.live_models import load_live_models

    app = _app(enabled=True, mailboxes=mailboxes, publisher=publisher)
    app.state.live_models = load_live_models(
        "config/live_models.yaml", environ={"DEMO_OPENROUTER_API_KEY": "k"}
    )

    def live(model: str | None) -> dict[str, Any]:
        options = {"model_profile": "live", "guard_mode": "C0", "live_model": model}
        return {**EMAIL, "run_options": options}

    async for client in _client(app):
        caps = (await client.get("/v1/demo/capabilities", headers=_headers(ORGS[0]))).json()
        keyed = await client.post(
            "/v1/demo/messages", headers=_headers(ORGS[0]), json=live("qwen2.5-7b-openrouter")
        )
        keyless = await client.post(
            "/v1/demo/messages", headers=_headers(ORGS[0]), json=live("gpt-4o-mini")
        )
        unnamed = await client.post("/v1/demo/messages", headers=_headers(ORGS[0]), json=live(None))

    assert caps["live_model_available"] is True
    assert {m["name"]: m["available"] for m in caps["live_models"]} == {
        "gpt-4o-mini": False,
        "qwen2.5-7b-openrouter": True,
        "llama-3.1-8b-openrouter": True,
    }
    assert keyed.status_code == status.HTTP_202_ACCEPTED
    envelope = publisher.publish.await_args_list[0].kwargs["envelope"]
    assert envelope.run_options["live_model"] == "qwen2.5-7b-openrouter"
    assert keyless.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert "DEMO_OPENAI_API_KEY" in keyless.json()["error"]
    # two keyed models and no LLM__* provider: a live run has to name one
    assert unnamed.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert publisher.publish.await_count == 1


async def test_an_organization_without_a_demo_mailbox_gets_409(
    client: AsyncClient, publisher: MagicMock
) -> None:
    response = await client.post("/v1/demo/messages", headers=_headers(uuid4()), json=EMAIL)

    assert response.status_code == status.HTTP_409_CONFLICT
    publisher.publish.assert_not_awaited()


async def test_the_organization_header_is_required(client: AsyncClient) -> None:
    response = await client.post("/v1/demo/messages", json=EMAIL)

    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_capabilities_name_the_tenants_demo_mailbox(
    client: AsyncClient, mailboxes: list[Mailbox]
) -> None:
    response = await client.get("/v1/demo/capabilities", headers=_headers(ORGS[2]))

    body = response.json()
    assert body["demo_mailbox_id"] == str(mailboxes[2].id)
    assert body["live_model_available"] is False
    assert body["guard_available"] is False


async def test_a_request_is_pending_until_its_job_exists(
    app: FastAPI, client: AsyncClient, mailboxes: list[Mailbox]
) -> None:
    request_id = uuid4()
    url = f"/v1/demo/requests/{request_id}"

    pending = await client.get(url, headers=_headers(ORGS[0]))
    key = derive_idempotency_key(
        organization_id=ORGS[0],
        mailbox_id=mailboxes[0].id,
        provider_message_id=f"demo-{request_id}",
        operation_type="normalize",
    )
    job, _ = await app.state.job_store.create_job(
        Job(
            organization_id=ORGS[0],
            idempotency_key=key,
            run_options={"model_profile": "fake", "guard_mode": "C0"},
        )
    )
    ingested = await client.get(url, headers=_headers(ORGS[0]))
    other_tenant = await client.get(url, headers=_headers(ORGS[1]))

    assert pending.json()["status"] == "pending"
    assert ingested.json()["status"] == "ingested"
    assert ingested.json()["job_id"] == str(job.id)
    assert ingested.json()["run_options"] == {
        "model_profile": "fake",
        "guard_mode": "C0",
        "live_model": None,  # ADR-0019: the job named no catalog model
    }
    assert other_tenant.json()["status"] == "pending"


# --- the mail-connector consumer ---------------------------------------------------------------


class _Publisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, JobEnvelope]] = []

    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        envelope: JobEnvelope,
        headers: dict[str, Any] | None = None,
    ) -> None:
        self.published.append((routing_key, envelope))


async def _envelope_via_api(mailboxes: list[Mailbox], org: UUID, body: dict[str, Any]) -> Any:
    publisher = MagicMock()
    publisher.publish = AsyncMock()
    app = _app(enabled=True, mailboxes=mailboxes, publisher=publisher)
    async for client in _client(app):
        response = await client.post("/v1/demo/messages", headers=_headers(org), json=body)
    assert response.status_code == status.HTTP_202_ACCEPTED
    return publisher.publish.await_args.kwargs["envelope"]


def _consumer(mailboxes: list[Mailbox]) -> tuple[DemoInjectConsumer, InMemoryJobStore, _Publisher]:
    store = InMemoryMailboxStore(mailboxes)
    jobs = InMemoryJobStore()
    publisher = _Publisher()
    orchestrator = SyncOrchestrator(
        checkpoint_store=InMemoryCheckpointStore(),
        storage_client=FakeObjectStorageClient(),
        publisher=publisher,
        mailbox_store=store,
        job_store=jobs,
    )
    return DemoInjectConsumer(orchestrator=orchestrator, mailbox_store=store), jobs, publisher


async def test_the_connector_creates_the_job_with_its_run_options(
    mailboxes: list[Mailbox],
) -> None:
    consumer, jobs, publisher = _consumer(mailboxes)

    for org in ORGS:
        envelope = await _envelope_via_api(mailboxes, org, EMAIL)
        await consumer.process_job(envelope, MagicMock())

    assert [rk for rk, _ in publisher.published] == ["email.normalize"] * 3
    for (_, normalize), org in zip(publisher.published, ORGS, strict=True):
        assert normalize.organization_id == str(org)
        assert normalize.run_options == {"model_profile": "fake", "guard_mode": "C3"}
        job = await jobs.get_job(org, UUID(normalize.job_id))
        assert job is not None and job.run_options == normalize.run_options


async def test_consecutive_demo_emails_to_one_mailbox_all_arrive(
    mailboxes: list[Mailbox],
) -> None:
    """Each demo email has its own history id, so the stored cursor never skips the next one."""
    consumer, _, publisher = _consumer(mailboxes)

    for _ in range(3):
        await consumer.process_job(await _envelope_via_api(mailboxes, ORGS[0], EMAIL), MagicMock())

    assert len(publisher.published) == 3
    assert len({env.message_id for _, env in publisher.published}) == 3


async def test_the_connector_refuses_a_mailbox_that_is_not_a_demo_one(
    mailboxes: list[Mailbox],
) -> None:
    consumer, _, publisher = _consumer(mailboxes)
    envelope = await _envelope_via_api(mailboxes, ORGS[0], EMAIL)
    envelope.mailbox_id = str(mailboxes[3].id)  # ORGS[0]'s real imap mailbox

    with pytest.raises(FatalError, match="not a demo mailbox"):
        await consumer.process_job(envelope, MagicMock())
    assert publisher.published == []


async def test_the_connector_refuses_another_tenants_mailbox(mailboxes: list[Mailbox]) -> None:
    consumer, _, publisher = _consumer(mailboxes)
    envelope = await _envelope_via_api(mailboxes, ORGS[0], EMAIL)
    envelope.organization_id = str(ORGS[1])

    with pytest.raises(FatalError, match="does not belong"):
        await consumer.process_job(envelope, MagicMock())
    assert publisher.published == []


async def test_the_consumer_runs_only_when_the_demo_is_enabled() -> None:
    off = await build_components(fake_worker_resources(MailConnectorSettings(_env_file=None)))
    on = await build_components(
        fake_worker_resources(
            MailConnectorSettings(_env_file=None, demo=DemoSettings(enabled=True))
        )
    )

    assert len(on) == len(off) + 1
    consumer = on[-1].__self__  # type: ignore[attr-defined]
    assert isinstance(consumer, DemoInjectConsumer)
    assert consumer.queue_name == "mail.demo.inject"
    assert consumer.prefetch_count == 1


# --- the demo adapter ------------------------------------------------------------------------


def test_a_subject_cannot_add_a_header() -> None:
    raw = build_demo_mime(
        sender_email="a@example.com",
        sender_name=None,
        recipient="demo@acme.com",
        subject="Hi\r\nBcc: victim@example.com",
        body="x",
        message_id="<m@demo.invalid>",
        date=datetime.now(UTC),
    )

    parsed = message_from_bytes(raw, policy=policy.default)
    assert parsed["Bcc"] is None
    assert "Bcc: victim" in parsed["Subject"]


async def test_the_demo_mailbox_records_a_reply_and_sends_nothing(
    mailboxes: list[Mailbox],
) -> None:
    mailbox = mailboxes[0]
    adapter = get_adapter_for_mailbox(mailbox)
    reply = OutboundReply(
        thread_id="demo-thread-1",
        mailbox_id=mailbox.id,
        organization_id=mailbox.organization_id,
        to=[],
        subject="Re: Invoice",
        body_text="Net 30.",
    )

    ref = await adapter.create_draft(mailbox, reply)
    await adapter.send_draft(mailbox, ref.provider_draft_id)

    assert isinstance(adapter, DemoMailAdapter)
    assert get_adapter_for_mailbox(mailbox) is adapter  # a redelivery sees the same drafts
    assert adapter.sent_count == 1
