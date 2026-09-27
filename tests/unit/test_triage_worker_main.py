"""Unit tests for the triage worker composition root (RA.9)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import MethodType
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.core.settings import TriageSettings, TriageWorkerSettings
from packages.db.draft import InMemoryDraftStore
from packages.db.job import InMemoryJobStore
from packages.db.message import InMemoryMessageStore
from packages.domain.entities import EmailAddress, Job, NormalizedMessage
from packages.domain.state_machine import JobState
from packages.llm.fake import FakeLLMProvider
from services.triage_worker.classifier import MLClassifier
from services.triage_worker.consumer import TriageConsumer
from services.triage_worker.main import (
    TriageWorkerConfigError,
    build_components,
    build_triage_consumer,
)
from tests.stubs.worker_resources import fake_worker_resources

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo_cwd(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(REPO_ROOT)  # settings paths are cwd-relative, like WORKDIR /app
    return REPO_ROOT


def _settings(**triage_overrides: str) -> TriageWorkerSettings:
    triage = TriageSettings(**triage_overrides) if triage_overrides else TriageSettings()
    return TriageWorkerSettings(_env_file=None, triage=triage)


def _publisher() -> MagicMock:
    pub = MagicMock(spec=MessagePublisher)
    pub.publish = AsyncMock()
    return pub


def _build(settings: TriageWorkerSettings, **stores: Any) -> TriageConsumer:
    return build_triage_consumer(
        settings,
        publisher=stores.get("publisher", _publisher()),
        job_store=stores.get("job_store", InMemoryJobStore()),
        message_store=stores.get("message_store", InMemoryMessageStore()),
        draft_store=stores.get("draft_store", InMemoryDraftStore()),
    )


def test_templates_path_setting_default() -> None:
    assert TriageSettings().templates_path == "config/templates.yaml"


def test_build_wires_components_from_settings(repo_cwd: Path) -> None:
    settings = _settings()
    job_store, draft_store = InMemoryJobStore(), InMemoryDraftStore()
    consumer = _build(settings, job_store=job_store, draft_store=draft_store)

    assert consumer.queue_name == "email.triage"
    assert consumer.route_exchange == "email.route"
    assert consumer.prefetch_count == settings.concurrency.triage_worker_concurrency
    assert consumer.cascade.rule_engine.rules_path == Path(settings.triage.rules_path)
    assert consumer.cascade.rule_engine.rules_count > 0
    assert isinstance(consumer.cascade.ml_classifier, MLClassifier)
    assert isinstance(consumer.cascade.llm_classifier.provider, FakeLLMProvider)
    assert consumer.cascade.gate is consumer.gate
    assert consumer.job_store is job_store and consumer.gate.job_store is job_store
    assert consumer.gate.draft_store is draft_store
    assert consumer.gate.template_registry is not None
    assert consumer.gate.template_registry.find_template("acknowledgement", "receipt_confirmation")
    assert (
        consumer.cascade.threshold_manager.get_threshold("ml")
        == settings.triage.ml_confidence_threshold
    )


@pytest.mark.parametrize(
    ("field", "label"),
    [("rules_path", "rules"), ("templates_path", "templates"), ("ml_model_path", "ML classifier")],
)
def test_build_fails_fast_when_asset_missing(
    repo_cwd: Path, tmp_path: Path, field: str, label: str
) -> None:
    settings = _settings(**{field: str(tmp_path / "missing.file")})
    with pytest.raises(TriageWorkerConfigError, match=label):
        _build(settings)


def test_build_rejects_rules_file_with_zero_rules(repo_cwd: Path, tmp_path: Path) -> None:
    bad = tmp_path / "rules.yaml"
    bad.write_text("rules: [unclosed", encoding="utf-8")
    with pytest.raises(TriageWorkerConfigError, match="zero rules"):
        _build(_settings(rules_path=str(bad)))


def test_build_rejects_template_with_unresolvable_body(repo_cwd: Path, tmp_path: Path) -> None:
    tpl = tmp_path / "templates.yaml"
    tpl.write_text(
        "templates:\n"
        "  - id: t1\n    version: v1\n"
        "    match: {category: acknowledgement, intent: receipt_confirmation}\n"
        "    subject: 'Re: x'\n    body: prompts/templates/does_not_exist.txt\n",
        encoding="utf-8",
    )
    with pytest.raises(TriageWorkerConfigError, match="does_not_exist.txt"):
        _build(_settings(templates_path=str(tpl)))


async def test_composed_consumer_routes_email_worker_envelope(repo_cwd: Path) -> None:
    """Real rules+ML+gate path on the exact envelope shape the email worker emits."""
    job_store, message_store, publisher = InMemoryJobStore(), InMemoryMessageStore(), _publisher()
    consumer = _build(
        _settings(), job_store=job_store, message_store=message_store, publisher=publisher
    )

    org, mbx, mid, tid = uuid4(), uuid4(), uuid4(), uuid4()
    msg = NormalizedMessage(
        message_id=mid,
        thread_id=tid,
        mailbox_id=mbx,
        organization_id=org,
        provider="gmail",
        provider_message_id="prov-urgent-1",
        sender=EmailAddress(email="client@enterprise.com", name="Client"),
        received_at=datetime.now(UTC),
        subject="Urgent: Overdue payment failure on account",
        body_text="Your account balance is past due with repeated payment failure. Please advise.",
    )
    await message_store.insert_message(msg)
    job = Job(
        id=uuid4(),
        organization_id=org,
        idempotency_key=f"idem-{uuid4()}",
        job_type="email_pipeline",
        state=JobState.NORMALIZED,
        message_id=mid,
        thread_id=tid,
    )
    await job_store.create_job(job)

    envelope = JobEnvelope(
        job_id=str(job.id),
        idempotency_key=f"triage:{org}:{mbx}:prov-urgent-1",
        organization_id=str(org),
        mailbox_id=str(mbx),
        message_id=str(mid),
        thread_id=str(tid),
        job_type="triage_email",
        payload={
            "message_id": str(mid),
            "thread_id": str(tid),
            "organization_id": str(org),
            "mailbox_id": str(mbx),
            "provider_message_id": "prov-urgent-1",
            "direction": "inbound",
            "received_at": msg.received_at.isoformat(),
        },
    )
    await consumer.process_job(envelope, MagicMock())

    publisher.publish.assert_awaited_once()
    kwargs = publisher.publish.await_args.kwargs
    assert kwargs["exchange_name"] == "email.route"
    assert kwargs["routing_key"] == "email.billing.priority"
    assert kwargs["envelope"].job_type == "generate_reply"
    assert kwargs["envelope"].classification["decided_by"] == "rule"
    stored = await job_store.get_job(org, job.id)
    assert stored is not None and stored.state == JobState.QUEUED


async def test_build_components_returns_consumer_start(repo_cwd: Path) -> None:
    settings = _settings()
    start_fns = await build_components(fake_worker_resources(settings))
    assert len(start_fns) == 1
    start = start_fns[0]
    assert isinstance(start, MethodType)
    consumer = start.__self__
    assert isinstance(consumer, TriageConsumer)
    assert consumer.queue_name == settings.broker.queue_triage
