"""The category retrieval floor (task 7.28; R6.6, R6.9, R12.4; design.md section 5.3).

Accepted by the owner, 2026-10-01 00:42 (ADR-0012 decision 12): ADR-0013
(docs/adr/0013-category-retrieval-floor-and-benchmark-category-filter.md).

The live v2 smoke of 2026-09-30 and 2026-10-01 (gpt-4o-mini) saw the stage-3 LLM answer
``retrieval_required=false`` for 9 of 9 company-policy questions (warranty period, refund fee,
password reset, shipping redirect, discount, policy), so hybrid retrieval and the reranker never
ran. ``config/categories.yaml`` declares ``default_retrieval_required`` per category, and nothing
applied it to a stage's result.

The gate, where a required reply is finally routed to AI generation, now raises
``retrieval_required`` to the category's default, whichever stage decided (rule, ml, llm or the
safe default). It never touches the two zero-AI outcomes: no reply (R6.5) and a template reply
(R6.13). ``TRIAGE__CATEGORY_RETRIEVAL_FLOOR=false`` switches it off.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
import yaml
from dotenv import dotenv_values
from prometheus_client import CollectorRegistry

from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.routing import load_categories_from_yaml
from packages.context.assembly import ThreadContextAssembler
from packages.context.builder import ContextBuilder
from packages.core.settings import AppSettings, SummarizationSettings, TriageSettings
from packages.db.classification import InMemoryClassificationStore
from packages.db.job import InMemoryJobStore
from packages.domain.entities import Classification, EmailAddress, Job, NormalizedMessage
from packages.domain.rules import Rule, RuleEngine
from packages.domain.state_machine import JobState
from packages.domain.taxonomy import TaxonomyRegistry
from packages.domain.templates import TemplateDefinition, TemplateRegistry
from packages.llm.fake import FakeLLMProvider
from packages.observability.metrics import (
    PipelineMetrics,
    create_pipeline_metrics,
    generate_metrics_payload,
)
from packages.retrieval.fake import FakeSearchBackend
from packages.retrieval.query_builder import RetrievalQueryBuilder
from packages.retrieval.retriever import HybridRetriever
from services.ai_worker.consumer import classification_from_snapshot
from services.triage_worker.cascade import CascadingTriageEngine
from services.triage_worker.classifier import MLClassifier
from services.triage_worker.consumer import TriageConsumer
from services.triage_worker.gate import EarlyExitGate, GateAction
from services.triage_worker.llm_classifier import LLMTriageClassifier
from services.triage_worker.rules import (
    HotReloadableRuleEngine,
    load_rules_from_file,
    load_rules_from_yaml,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FLOOR_MARKER = "retrieval_required_from_category"
"""Recorded in the gate event payload and in the routed classification's ``raw`` when the floor
raised ``retrieval_required`` (the stage said false, the category's default is true)."""
# The categories of config/categories.yaml that reply and whose default is to retrieve.
RETRIEVING_CATEGORIES = ("support", "sales", "billing", "administration", "general_inquiry")


def _job(state: JobState = JobState.CLASSIFIED) -> Job:
    return Job(
        id=uuid4(),
        organization_id=uuid4(),
        message_id=uuid4(),
        thread_id=uuid4(),
        state=state,
    )


def _classification(
    category: str = "support",
    *,
    decided_by: str = "llm",
    reply_required: bool = True,
    workflow_hint: str = "ai",
    retrieval_required: bool = False,
    intent: str | None = "warranty_question",
) -> Classification:
    return Classification(
        category=category,
        intent=intent,
        reply_required=reply_required,
        workflow_hint=workflow_hint,
        retrieval_required=retrieval_required,
        confidence=0.9,
        decided_by=decided_by,
    )


def test_the_categories_this_file_relies_on_are_the_ones_the_config_declares() -> None:
    """If config/categories.yaml changes a default, the tests below must be looked at again."""
    declared = yaml.safe_load((REPO_ROOT / "config" / "categories.yaml").read_text("utf-8"))
    replying = {
        entry["category"]: entry["default_retrieval_required"]
        for entry in declared["categories"]
        if entry["default_reply_required"]
    }
    assert {name for name, retrieves in replying.items() if retrieves} == set(RETRIEVING_CATEGORIES)
    assert [name for name, retrieves in replying.items() if not retrieves] == ["scheduling"]


# --- the gate, one required reply routed to AI generation ---------------------------------------


@pytest.mark.parametrize("category", RETRIEVING_CATEGORIES)
def test_a_reply_routed_to_ai_retrieves_when_its_category_does(category: str) -> None:
    classification = _classification(category, retrieval_required=False)

    decision = EarlyExitGate().evaluate_decision(_job(), classification)

    assert decision.action is GateAction.PROCEED_RAG
    assert decision.job.state == JobState.QUEUED
    assert decision.retrieval_required is True
    assert (decision.should_embed, decision.should_retrieve, decision.should_rerank) == (
        True,
        True,
        True,
    )
    assert decision.should_generate is True
    # What the ai-worker reads (the route envelope carries decision.classification, R7.3) ...
    assert decision.classification.retrieval_required is True
    assert decision.classification.raw[FLOOR_MARKER] is True
    # ... and what the job's timeline records.
    assert decision.event.payload["retrieval_required"] is True
    assert decision.event.payload[FLOOR_MARKER] is True


def test_a_category_that_does_not_retrieve_keeps_the_stage_answer() -> None:
    # scheduling is the one replying category whose default is false (config/categories.yaml).
    classification = _classification("scheduling", intent="meeting_request")

    decision = EarlyExitGate().evaluate_decision(_job(), classification)

    assert decision.action is GateAction.PROCEED_NO_RAG
    assert decision.retrieval_required is False
    assert (decision.should_embed, decision.should_retrieve, decision.should_rerank) == (
        False,
        False,
        False,
    )
    assert decision.should_generate is True
    assert decision.event.payload["retrieval_required"] is False
    assert decision.event.payload[FLOOR_MARKER] is False
    assert FLOOR_MARKER not in decision.classification.raw


def test_a_stage_that_already_asked_for_retrieval_is_not_reported_as_raised() -> None:
    classification = _classification("support", retrieval_required=True)

    decision = EarlyExitGate().evaluate_decision(_job(), classification)

    assert decision.action is GateAction.PROCEED_RAG
    assert decision.event.payload[FLOOR_MARKER] is False
    assert FLOOR_MARKER not in decision.classification.raw


def test_the_floor_only_raises_a_stage_may_ask_for_retrieval_in_any_category() -> None:
    classification = _classification("scheduling", retrieval_required=True)

    decision = EarlyExitGate().evaluate_decision(_job(), classification)

    assert decision.action is GateAction.PROCEED_RAG
    assert decision.retrieval_required is True
    assert decision.event.payload[FLOOR_MARKER] is False


def test_the_floor_hands_on_a_copy_and_leaves_the_stage_result_as_it_was() -> None:
    # R6.7: the cascade persists the stage's own result, so the gate must not rewrite it.
    classification = _classification("support", retrieval_required=False)

    decision = EarlyExitGate().evaluate_decision(_job(), classification)

    assert decision.classification is not classification
    assert classification.retrieval_required is False
    assert classification.raw == {}


@pytest.mark.parametrize("retrieval_required", [True, False])
@pytest.mark.parametrize("workflow_hint", ["ai", "none", "template"])
@pytest.mark.parametrize("category", RETRIEVING_CATEGORIES)
def test_no_reply_required_exits_early_with_no_retrieval_in_any_category(
    category: str, workflow_hint: str, retrieval_required: bool
) -> None:
    # R6.5: only reply_required decides an early exit; the floor is never consulted.
    classification = _classification(
        category,
        reply_required=False,
        workflow_hint=workflow_hint,
        retrieval_required=retrieval_required,
    )

    decision = EarlyExitGate().evaluate_decision(_job(), classification)

    assert decision.action is GateAction.EARLY_EXIT
    assert decision.job.state == JobState.COMPLETED
    assert decision.retrieval_required is False
    assert not (decision.should_embed or decision.should_retrieve or decision.should_rerank)
    assert not decision.should_generate
    assert FLOOR_MARKER not in decision.event.payload
    assert FLOOR_MARKER not in decision.classification.raw


def _password_reset_registry() -> TemplateRegistry:
    registry = TemplateRegistry()
    registry.register(
        TemplateDefinition(
            id="admin-password-reset-v1",
            version="v1",
            category="administration",
            intent="password_reset",
            subject="Re: {{ subject }}",
            body="Use the reset link on the sign-in page.",
        )
    )
    return registry


@pytest.mark.parametrize("retrieval_required", [True, False])
def test_a_template_reply_never_retrieves_even_in_a_category_that_does(
    retrieval_required: bool,
) -> None:
    # R6.13: administration retrieves by default, but a matched template is zero AI work.
    gate = EarlyExitGate(template_registry=_password_reset_registry())
    classification = _classification(
        "administration",
        intent="password_reset",
        workflow_hint="template",
        retrieval_required=retrieval_required,
    )

    decision = gate.evaluate_decision(_job(), classification, message={"subject": "Reset"})

    assert decision.action is GateAction.TEMPLATE_REPLY
    assert decision.job.state == JobState.DRAFTED
    assert decision.retrieval_required is False
    assert not (decision.should_embed or decision.should_retrieve or decision.should_rerank)
    assert not decision.should_generate
    assert FLOOR_MARKER not in decision.event.payload
    assert FLOOR_MARKER not in decision.classification.raw


@pytest.mark.parametrize("registry", [TemplateRegistry(), None], ids=["no-match", "no-registry"])
def test_a_template_hint_with_no_template_falls_back_to_ai_and_then_retrieves(
    registry: TemplateRegistry | None,
) -> None:
    # R6.14: the fallback is AI generation, the outcome the floor applies to. Administration is
    # declared workflow_hint=template, retrieval true: a password question no template answers.
    gate = EarlyExitGate(template_registry=registry)
    classification = _classification(
        "administration", intent="password_reset", workflow_hint="template"
    )

    decision = gate.evaluate_decision(_job(), classification)

    assert decision.action is GateAction.PROCEED_RAG
    assert decision.workflow_hint == "ai"
    assert decision.retrieval_required is True
    assert decision.event.payload[FLOOR_MARKER] is True


def test_a_none_hint_beside_a_required_reply_goes_to_ai_and_retrieves() -> None:
    # The smoke's shape: reply_required=true, workflow_hint='none', retrieval_required=false.
    classification = _classification(
        "general_inquiry", intent="warranty_question", workflow_hint="none"
    )

    decision = EarlyExitGate().evaluate_decision(_job(), classification)

    assert decision.action is GateAction.PROCEED_RAG
    assert decision.workflow_hint == "ai"
    assert decision.event.payload["classified_workflow_hint"] == "none"
    assert decision.event.payload[FLOOR_MARKER] is True


def test_the_safe_default_already_retrieves_and_is_not_reported_as_raised() -> None:
    # R6.11: all stages failed; general_inquiry, reply_required=true, retrieval_required=true.
    safe_default = LLMTriageClassifier.safe_default(error_message="stage 3 failed")

    decision = EarlyExitGate().evaluate_decision(_job(), safe_default)

    assert decision.action is GateAction.PROCEED_RAG
    assert decision.event.payload[FLOOR_MARKER] is False


# --- the switch ---------------------------------------------------------------------------------


def test_the_floor_can_be_switched_off() -> None:
    gate = EarlyExitGate(category_retrieval_floor=False)
    classification = _classification("support", retrieval_required=False)

    decision = gate.evaluate_decision(_job(), classification)

    assert decision.action is GateAction.PROCEED_NO_RAG
    assert decision.retrieval_required is False
    assert decision.event.payload[FLOOR_MARKER] is False
    assert FLOOR_MARKER not in decision.classification.raw


def test_the_floor_is_on_by_default_and_only_the_setting_turns_it_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert EarlyExitGate().category_retrieval_floor is True
    assert TriageSettings().category_retrieval_floor is True
    monkeypatch.setenv("TRIAGE__CATEGORY_RETRIEVAL_FLOOR", "false")
    assert AppSettings(_env_file=None).triage.category_retrieval_floor is False
    monkeypatch.setenv("TRIAGE__CATEGORY_RETRIEVAL_FLOOR", "true")
    assert AppSettings(_env_file=None).triage.category_retrieval_floor is True


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def test_compose_forwards_the_switch_to_the_triage_worker_from_the_host_env() -> None:
    # Compose forwards no TRIAGE__* otherwise, so without this line .env could not flip it in a
    # container. Value-less (or interpolated): unset, the settings default applies.
    compose = yaml.safe_load(_read("docker-compose.yml"))
    environment = compose["services"]["triage-worker"]["environment"]
    assert "TRIAGE__CATEGORY_RETRIEVAL_FLOOR" in environment
    forwarded = environment["TRIAGE__CATEGORY_RETRIEVAL_FLOOR"]
    assert forwarded is None or str(forwarded).startswith("${TRIAGE__CATEGORY_RETRIEVAL_FLOOR")


def test_env_example_documents_the_switch_with_its_default() -> None:
    active = dotenv_values(REPO_ROOT / ".env.example")
    assert active["TRIAGE__CATEGORY_RETRIEVAL_FLOOR"] == "true"  # never blank: Compose forwards it


def test_the_configuration_reference_documents_the_switch_with_its_default() -> None:
    row = re.search(
        r"^\| `TRIAGE__CATEGORY_RETRIEVAL_FLOOR` \| `boolean` \| `true` \|",
        _read("docs/configuration.md"),
        re.M,
    )
    assert row, "docs/configuration.md needs a TRIAGE__CATEGORY_RETRIEVAL_FLOOR row (boolean, true)"


# --- where the category's default comes from ----------------------------------------------------


def test_a_category_alias_takes_the_default_of_its_category() -> None:
    # 'technical_support' is an alias of support in config/categories.yaml.
    decision = EarlyExitGate().evaluate_decision(_job(), _classification("technical_support"))

    assert decision.action is GateAction.PROCEED_RAG
    assert decision.event.payload[FLOOR_MARKER] is True


def test_a_category_the_taxonomy_does_not_know_keeps_the_stage_answer() -> None:
    # No default to apply: the stage's own answer stands.
    decision = EarlyExitGate().evaluate_decision(_job(), _classification("not_in_the_taxonomy"))

    assert decision.action is GateAction.PROCEED_NO_RAG
    assert decision.event.payload[FLOOR_MARKER] is False


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ({"default_retrieval_required": True}, GateAction.PROCEED_RAG),
        ({"default_retrieval_required": False}, GateAction.PROCEED_NO_RAG),
        ({}, GateAction.PROCEED_RAG),  # register_from_dict: an omitted default is true
    ],
)
def test_a_category_added_by_configuration_brings_its_own_default(
    declared: dict[str, Any], expected: GateAction
) -> None:
    taxonomy = TaxonomyRegistry()
    taxonomy.register_from_dict({"category": "warranty", **declared})
    gate = EarlyExitGate(taxonomy=taxonomy)

    decision = gate.evaluate_decision(_job(), _classification("warranty"))

    assert decision.action is expected


def test_the_default_comes_from_the_categories_file_so_an_operator_can_change_it(
    tmp_path: Path,
) -> None:
    # R6.9 (per category, without a code change): support declares false, so it is not raised.
    override = tmp_path / "categories.yaml"
    override.write_text(
        yaml.safe_dump(
            {
                "categories": [
                    {"category": "support", "default_retrieval_required": False},
                    {"category": "sales", "default_retrieval_required": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    taxonomy = TaxonomyRegistry()
    load_categories_from_yaml(override, registry=taxonomy)
    gate = EarlyExitGate(taxonomy=taxonomy)

    support = gate.evaluate_decision(_job(), _classification("support"))
    sales = gate.evaluate_decision(_job(), _classification("sales"))

    assert support.action is GateAction.PROCEED_NO_RAG
    assert sales.action is GateAction.PROCEED_RAG


def test_a_category_registered_after_the_gate_was_built_is_honoured() -> None:
    # The worker loads config/categories.yaml while it starts; the gate reads the registry when
    # it decides, not when it is built.
    taxonomy = TaxonomyRegistry()
    gate = EarlyExitGate(taxonomy=taxonomy)
    taxonomy.register_from_dict({"category": "late_category", "default_retrieval_required": True})

    decision = gate.evaluate_decision(_job(), _classification("late_category"))

    assert decision.action is GateAction.PROCEED_RAG


# --- the persisted gate (the worker's path) -----------------------------------------------------


async def test_the_persisted_gate_raises_and_records_it_on_the_job_timeline() -> None:
    store = InMemoryJobStore()
    job = _job()
    await store.create_job(job)
    gate = EarlyExitGate(job_store=store)

    decision = await gate.evaluate_and_persist(job, _classification("billing"))

    assert decision.action is GateAction.PROCEED_RAG
    assert decision.classification.raw[FLOOR_MARKER] is True
    persisted = await store.get_job(job.organization_id, job.id)
    assert persisted is not None and persisted.state == JobState.QUEUED
    queued = [
        event
        for event in await store.list_events_for_job(job.organization_id, job.id)
        if event.state_to == JobState.QUEUED.value
    ]
    assert len(queued) == 1
    assert queued[0].payload is not None
    assert queued[0].payload["retrieval_required"] is True
    assert queued[0].payload[FLOOR_MARKER] is True


async def test_the_persisted_gate_leaves_a_switched_off_floor_alone() -> None:
    store = InMemoryJobStore()
    job = _job()
    await store.create_job(job)
    gate = EarlyExitGate(job_store=store, category_retrieval_floor=False)

    decision = await gate.evaluate_and_persist(job, _classification("billing"))

    assert decision.action is GateAction.PROCEED_NO_RAG
    queued = [
        event
        for event in await store.list_events_for_job(job.organization_id, job.id)
        if event.state_to == JobState.QUEUED.value
    ]
    assert queued[0].payload is not None
    assert queued[0].payload[FLOOR_MARKER] is False


# --- each stage of the cascade, through the engine and the gate ---------------------------------

POLICY_QUESTION = {
    "subject": "What is the warranty period for the X200 vacuum?",
    "body_text": "Hello, how long is the warranty on the X200 vacuum cleaner I bought in March?",
    "sender_email": "customer@example.com",
}


def _llm_classifier(**answer: Any) -> LLMTriageClassifier:
    response = {
        "category": "general_inquiry",
        "intent": "warranty_question",
        "priority": "normal",
        "reply_required": True,
        "workflow_hint": "ai",
        "retrieval_required": False,
        "confidence": 0.92,
        "reasoning": "A company-policy question.",
        **answer,
    }
    return LLMTriageClassifier(provider=FakeLLMProvider(default_response=response))


async def test_a_rule_that_says_no_retrieval_is_raised_to_the_category_default() -> None:
    rule = Rule.from_dict(
        {
            "id": "warranty-questions",
            "when": {"subject": {"contains": "warranty"}},
            "then": {
                "category": "support",
                "reply_required": True,
                "workflow_hint": "ai",
                "retrieval_required": False,
                "confidence": 0.99,
            },
        }
    )
    engine = CascadingTriageEngine(rule_engine=HotReloadableRuleEngine(initial_rules=[rule]))

    cascade, decision = await engine.triage_and_gate(_job(JobState.NORMALIZED), POLICY_QUESTION)

    assert cascade.decided_stage == "rule"
    assert cascade.classification.retrieval_required is False  # the stage's own result
    assert decision.action is GateAction.PROCEED_RAG
    assert decision.event.payload[FLOOR_MARKER] is True


async def test_a_rule_that_leaves_retrieval_out_already_asks_for_it() -> None:
    # RuleAction defaults retrieval_required to reply_required, so nothing is raised.
    rule = Rule.from_dict(
        {
            "id": "warranty-questions",
            "when": {"subject": {"contains": "warranty"}},
            "then": {"category": "support", "reply_required": True, "confidence": 0.99},
        }
    )
    engine = CascadingTriageEngine(rule_engine=HotReloadableRuleEngine(initial_rules=[rule]))

    cascade, decision = await engine.triage_and_gate(_job(JobState.NORMALIZED), POLICY_QUESTION)

    assert cascade.decided_stage == "rule"
    assert cascade.classification.retrieval_required is True
    assert decision.action is GateAction.PROCEED_RAG
    assert decision.event.payload[FLOOR_MARKER] is False


async def test_an_ml_result_that_says_no_retrieval_is_raised_to_the_category_default() -> None:
    ml = MagicMock(spec=MLClassifier)
    ml.classify.return_value = Classification(
        category="billing",
        priority="normal",
        reply_required=True,
        workflow_hint="ai",
        retrieval_required=False,
        confidence=0.95,
        decided_by="ml",
    )
    engine = CascadingTriageEngine(
        rule_engine=HotReloadableRuleEngine(initial_rules=[]), ml_classifier=ml
    )

    cascade, decision = await engine.triage_and_gate(_job(JobState.NORMALIZED), POLICY_QUESTION)

    assert cascade.decided_stage == "ml"
    assert decision.action is GateAction.PROCEED_RAG
    assert decision.event.payload[FLOOR_MARKER] is True


@pytest.mark.parametrize("workflow_hint", ["ai", "none"])
async def test_an_llm_result_that_says_no_retrieval_is_raised_to_the_category_default(
    workflow_hint: str,
) -> None:
    # The smoke: a company-policy question, retrieval_required=false from the stage-3 model.
    engine = CascadingTriageEngine(
        rule_engine=HotReloadableRuleEngine(initial_rules=[]),
        llm_classifier=_llm_classifier(workflow_hint=workflow_hint),
    )

    cascade, decision = await engine.triage_and_gate(_job(JobState.NORMALIZED), POLICY_QUESTION)

    assert cascade.decided_stage == "llm"
    assert cascade.classification.retrieval_required is False
    assert decision.action is GateAction.PROCEED_RAG
    assert decision.retrieval_required is True
    assert decision.event.payload[FLOOR_MARKER] is True


async def test_the_persisted_classification_is_the_stage_result_not_the_floored_one() -> None:
    # R6.7: the classification row is the stage's output; the floor is the gate's routing policy.
    store = InMemoryClassificationStore()
    engine = CascadingTriageEngine(
        rule_engine=HotReloadableRuleEngine(initial_rules=[]),
        llm_classifier=_llm_classifier(),
        classification_store=store,
    )
    job = _job(JobState.NORMALIZED)
    organization_id, message_id = job.organization_id, job.message_id
    assert message_id is not None

    cascade, decision = await engine.triage_and_gate(
        job,
        POLICY_QUESTION,
        organization_id=organization_id,
        message_id=message_id,
        persist=True,
    )

    row = await store.get_latest_classification_by_message(organization_id, message_id)
    assert row is not None
    assert row.retrieval_required is False
    assert FLOOR_MARKER not in row.raw
    assert decision.retrieval_required is True
    assert cascade.persisted_id == row.id


async def test_the_safe_default_after_every_stage_failed_retrieves_without_a_floor() -> None:
    llm = LLMTriageClassifier(provider=FakeLLMProvider(default_response={"category": 1}))
    engine = CascadingTriageEngine(
        rule_engine=HotReloadableRuleEngine(initial_rules=[]), llm_classifier=llm
    )

    cascade, decision = await engine.triage_and_gate(_job(JobState.NORMALIZED), POLICY_QUESTION)

    assert cascade.decided_stage == "default"
    assert decision.action is GateAction.PROCEED_RAG
    assert decision.event.payload[FLOOR_MARKER] is False


# --- the worker end to end: triage consumer, route envelope, ai-worker retrieval ----------------


class _RecordingPublisher(MessagePublisher):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, JobEnvelope]] = []

    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        envelope: JobEnvelope,
        headers: dict[str, Any] | None = None,
    ) -> None:
        self.published.append((routing_key, envelope))


def _policy_message(organization_id: UUID) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=organization_id,
        provider="fake",
        provider_message_id=f"prov-{uuid4()}",
        sender=EmailAddress(email="customer@example.com"),
        recipients=[EmailAddress(email="support@company.com")],
        subject=POLICY_QUESTION["subject"],
        body_text=POLICY_QUESTION["body_text"],
        body_text_clean=POLICY_QUESTION["body_text"],
        received_at=datetime.now(UTC),
    )


async def test_the_floor_reaches_the_ai_workers_retrieval_end_to_end() -> None:
    """Triage consumer -> route envelope -> the ai-worker's classification -> ContextBuilder.

    The job ends with the knowledge chunk that answers the question in its context, although the
    stage-3 model said retrieval_required=false: the floor reaches the real retrieval gate.
    """
    organization_id = uuid4()
    publisher = _RecordingPublisher()
    consumer = TriageConsumer(
        cascade=CascadingTriageEngine(
            rule_engine=HotReloadableRuleEngine(initial_rules=[]),
            llm_classifier=_llm_classifier(workflow_hint="none"),
        ),
        gate=EarlyExitGate(),
        publisher=publisher,
    )
    envelope = JobEnvelope(
        idempotency_key=f"idem-floor-{uuid4()}",
        job_type="triage",
        organization_id=str(organization_id),
        message_id=str(uuid4()),
        payload=dict(POLICY_QUESTION),
    )

    await consumer.process_job(envelope, MagicMock())

    assert len(publisher.published) == 1
    routing_key, routed = publisher.published[0]
    assert routing_key == "email.general_inquiry.normal"
    assert routed.classification["retrieval_required"] is True
    assert routed.classification["raw"][FLOOR_MARKER] is True

    # The ai-worker rebuilds the classification from that snapshot and builds the context.
    classification = classification_from_snapshot(routed.classification)
    backend = FakeSearchBackend()
    backend.add_chunk(
        chunk_id="CHUNK-WARRANTY",
        document_id="DOC-WARRANTY",
        organization_id=str(organization_id),
        content="The X200 vacuum cleaner has a two year warranty from the purchase date.",
        category="general_inquiry",
    )
    builder = ContextBuilder(
        thread_assembler=ThreadContextAssembler(settings=SummarizationSettings()),
        retriever=HybridRetriever(backend=backend),
        query_builder=RetrievalQueryBuilder(),
    )
    message = _policy_message(organization_id)
    job = Job(
        id=uuid4(),
        organization_id=organization_id,
        thread_id=message.thread_id,
        message_id=message.message_id,
        state=JobState.QUEUED,
    )

    package = await builder.build_context(job, message, classification, thread_messages=[message])

    assert [chunk.chunk_id for chunk in package.retrieved_chunks] == ["CHUNK-WARRANTY"]


# --- the floor is countable (CLAUDE.md section 3 item 5, R21.4) ---------------------------------


def _floor_count(
    metrics: PipelineMetrics, category: str, decided_by: str, org: UUID | str
) -> float:
    value = metrics.registry.get_sample_value(
        "retrieval_floor_applied_total",
        {"organization": str(org), "category": category, "decided_by": decided_by},
    )
    return value or 0.0


def test_a_job_the_floor_raised_is_counted_by_category_and_deciding_stage() -> None:
    metrics = create_pipeline_metrics(registry=CollectorRegistry())
    job = _job()

    EarlyExitGate(metrics=metrics).evaluate_decision(
        job, _classification("billing", decided_by="llm", retrieval_required=False)
    )

    assert _floor_count(metrics, "billing", "llm", job.organization_id) == 1.0


async def test_the_persisted_gate_counts_the_floor_once_the_transition_is_committed() -> None:
    metrics = create_pipeline_metrics(registry=CollectorRegistry())
    store = InMemoryJobStore()
    job = _job()
    await store.create_job(job)

    await EarlyExitGate(job_store=store, metrics=metrics).evaluate_and_persist(
        job, _classification("support", decided_by="rule")
    )

    assert _floor_count(metrics, "support", "rule", job.organization_id) == 1.0


@pytest.mark.parametrize(
    "case",
    [
        pytest.param({"retrieval_required": True}, id="stage-already-asked"),
        pytest.param({"category": "scheduling"}, id="category-does-not-retrieve"),
        pytest.param({"category": "no_such_category"}, id="unknown-category"),
        pytest.param({"reply_required": False, "workflow_hint": "none"}, id="no-reply"),
    ],
)
def test_a_job_the_floor_did_not_raise_is_not_counted(case: dict[str, Any]) -> None:
    metrics = create_pipeline_metrics(registry=CollectorRegistry())
    job = _job()
    args: dict[str, Any] = {"category": "support", "retrieval_required": False, **case}

    EarlyExitGate(metrics=metrics).evaluate_decision(job, _classification(**args))

    payload, _ = generate_metrics_payload(metrics.registry)
    assert b"retrieval_floor_applied_total{" not in payload


def test_a_switched_off_floor_counts_nothing() -> None:
    metrics = create_pipeline_metrics(registry=CollectorRegistry())
    job = _job()

    EarlyExitGate(metrics=metrics, category_retrieval_floor=False).evaluate_decision(
        job, _classification("support")
    )

    payload, _ = generate_metrics_payload(metrics.registry)
    assert b"retrieval_floor_applied_total{" not in payload


def test_the_observability_reference_lists_the_floor_counter() -> None:
    text = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")

    assert (
        "`retrieval_floor_applied_total` | Counter | `organization, category, decided_by`" in text
    )


# --- the shipped rules and the floor (ADR-0013, "Rules") -----------------------------------------


def _rules_that_reply_without_retrieval(engine: RuleEngine) -> list[str]:
    """Ids of rules the floor would overrule: they need a reply and say no retrieval."""
    return [
        rule.id
        for rule in engine.rules
        if rule.action.reply_required and not rule.action.retrieval_required
    ]


def test_no_shipped_rule_asks_for_a_reply_without_retrieval() -> None:
    # ADR-0013 records that the floor overrules a rule's explicit retrieval_required=false and
    # that this changes nothing today: every shipped rule with retrieval_required=false also has
    # reply_required=false, so it exits at the no-reply outcome before the floor. This holds that
    # claim: a rule edit that breaks it is overruled by the floor unnoticed otherwise.
    engine = load_rules_from_file(REPO_ROOT / "config" / "triage_rules.yaml")

    assert engine.rules, "the shipped rules did not load"
    assert _rules_that_reply_without_retrieval(engine) == []


def test_the_shipped_rules_check_would_catch_a_rule_that_replies_without_retrieval() -> None:
    engine = load_rules_from_yaml(
        "rules:\n"
        "  - id: fine\n"
        "    when: {subject: {contains: a}}\n"
        "    then: {category: support, reply_required: false, retrieval_required: false}\n"
        "  - id: overruled\n"
        "    when: {subject: {contains: b}}\n"
        "    then: {category: support, reply_required: true, retrieval_required: false}\n"
    )

    assert _rules_that_reply_without_retrieval(engine) == ["overruled"]
