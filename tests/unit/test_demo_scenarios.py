"""Demo scenario catalog (task 9.5, R25.2, R25.8, ADR-0018 decision 5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import yaml
from fastapi import status
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from packages.adapters.demo import demo_seed_mailbox, messages_from_payload
from packages.core.settings import APISettings, BrokerSettings, DemoSettings
from packages.db.job import InMemoryJobStore
from packages.db.mailbox import InMemoryMailboxStore
from packages.domain.entities import Mailbox
from services.api.demo_catalog import CATEGORIES_BY_GROUP, ScenarioCatalog, load_catalog
from services.api.main import create_app

CATALOG = Path("config/demo_scenarios.yaml")
CASES = Path("evaluation/datasets/mailguard/cases.jsonl")
ORG = uuid4()


@pytest.fixture(scope="module")
def catalog() -> ScenarioCatalog:
    return load_catalog(CATALOG)


def test_the_catalog_holds_every_planned_scenario(catalog: ScenarioCatalog) -> None:
    ids = [s.id for s in catalog.scenarios]

    assert ids == [f"S{i:02d}" for i in range(1, 15)] + [f"G{i:02d}" for i in range(1, 7)]
    assert all(s.title.vi and s.title.en for s in catalog.scenarios)


def test_guard_scenarios_compare_both_guard_modes(catalog: ScenarioCatalog) -> None:
    for scenario in catalog.scenarios:
        if scenario.group == "guard":
            assert scenario.kind == "comparison"
            assert [r.guard_mode for r in scenario.runs()] == ["C0", "C3"]


def test_copied_cases_keep_their_case_id_and_text(catalog: ScenarioCatalog) -> None:
    """ADR-0018 decision 5: a copied case names its source and, unless marked, keeps its text."""
    cases = {
        row["case_id"]: row
        for row in (json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines())
    }
    copied = [s for s in catalog.scenarios if s.source is not None]

    assert copied
    for scenario in copied:
        assert scenario.source is not None and scenario.email is not None
        case = cases[scenario.source.case_id]
        assert scenario.source.license == "MIT"
        assert case["source"] != "poisonedrag", "PoisonedRAG texts are non-commercial only"
        assert scenario.email.subject == case["email"]["subject"]
        if scenario.source.modified is None:
            assert scenario.email.body == case["email"]["body_text"]


def test_every_scenario_names_a_category_of_its_group(catalog: ScenarioCatalog) -> None:
    """Task 9.19: the console groups cards by category; each of the six has at least one."""
    for scenario in catalog.scenarios:
        assert scenario.category in CATEGORIES_BY_GROUP[scenario.group]
    assert {s.category for s in catalog.scenarios} == set().union(*CATEGORIES_BY_GROUP.values())


def test_every_guard_scenario_explains_itself(catalog: ScenarioCatalog) -> None:
    """Task 9.19: a guard card shows why the email is (or is not) an attack."""
    for scenario in catalog.scenarios:
        if scenario.group == "guard":
            assert scenario.rationale is not None
            assert scenario.rationale.vi and scenario.rationale.en


def test_attacks_mark_their_payload(catalog: ScenarioCatalog) -> None:
    """The attack text the console highlights is in the email (G01, G02) or a planted document."""
    for sid in ("G01", "G02", "G03", "G05"):
        scenario = catalog.get(sid)
        assert scenario is not None and scenario.highlights
    for sid in ("G03", "G05"):
        scenario = catalog.get(sid)
        assert scenario is not None
        poisoned = [d.content for d in scenario.knowledge if d.poisoned]
        assert poisoned and all(any(h in d for d in poisoned) for h in scenario.highlights)


def test_knowledge_is_filed_under_a_triage_category(catalog: ScenarioCatalog) -> None:
    """Retrieval keeps only documents of the email's triage category (R12.4, R10.4).

    A document filed under a category triage never returns (e.g. ``support`` for a return-policy
    question, which live triage files as ``general_inquiry``) is never retrieved, so the scenario
    silently runs without its knowledge.
    """
    taxonomy = {
        c["category"]
        for c in yaml.safe_load(Path("config/categories.yaml").read_text(encoding="utf-8"))[
            "categories"
        ]
    }
    for scenario in catalog.scenarios:
        for doc in scenario.knowledge:
            assert doc.category in taxonomy, (scenario.id, doc.title, doc.category)
    expected = {"S12": "general_inquiry", "G03": "general_inquiry", "G05": "sales"}
    for sid, category in expected.items():
        found = catalog.get(sid)
        assert found is not None
        assert {d.category for d in found.knowledge} == {category}, sid


def _entry(**extra: object) -> dict[str, object]:
    return {
        "id": "S98",
        "group": "pipeline",
        "category": "drafting",
        "kind": "email",
        "title": {"vi": "a", "en": "a"},
        "description": {"vi": "a", "en": "a"},
        "email": {"sender_email": "a@b.example", "subject": "Hello", "body": "Body text"},
        **extra,
    }


def test_a_minimal_entry_is_accepted() -> None:
    ScenarioCatalog.model_validate({"version": 1, "scenarios": [_entry(highlights=["Body"])]})


@pytest.mark.parametrize(
    "extra",
    [
        {"highlights": ["not in the email"]},
        {"highlights": [""]},
        {"category": "guard_email"},  # a guard category on a pipeline scenario
        {"category": "unknown"},
    ],
)
def test_a_bad_category_or_highlight_is_refused(extra: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ScenarioCatalog.model_validate({"version": 1, "scenarios": [_entry(**extra)]})


def test_a_malformed_catalog_is_refused() -> None:
    with pytest.raises(ValidationError):
        ScenarioCatalog.model_validate(
            {
                "version": 1,
                "scenarios": [
                    {
                        "id": "S99",
                        "group": "pipeline",
                        "kind": "flow",
                        "title": {"vi": "a", "en": "a"},
                        "description": {"vi": "a", "en": "a"},
                    }
                ],
            }
        )


def _app() -> tuple[Any, MagicMock]:
    publisher = MagicMock()
    publisher.publish = AsyncMock()
    publisher.settings = BrokerSettings()
    app = create_app(
        settings=APISettings(_env_file=None, demo=DemoSettings(enabled=True)),
        lifespan_enabled=False,
    )
    row = demo_seed_mailbox(
        mailbox_id=uuid4(), organization_id=ORG, address="demo@acme.com", display_name="Demo"
    )
    app.state.mailbox_store = InMemoryMailboxStore([Mailbox(**row)])
    app.state.job_store = InMemoryJobStore()
    app.state.publisher = publisher
    return app, publisher


async def _post(path: str, json_body: dict[str, object] | None = None) -> tuple[Any, MagicMock]:
    app, publisher = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(path, headers={"X-Organization-ID": str(ORG)}, json=json_body)
    return response, publisher


async def test_the_catalog_is_served() -> None:
    app, _ = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/v1/demo/scenarios", headers={"X-Organization-ID": str(ORG)})

    assert response.status_code == status.HTTP_200_OK
    assert len(response.json()) == 20


async def test_a_comparison_makes_two_independent_jobs() -> None:
    response, publisher = await _post("/v1/demo/scenarios/G04/run")

    assert response.status_code == status.HTTP_202_ACCEPTED
    envelopes = [call.kwargs["envelope"] for call in publisher.publish.await_args_list]
    assert [e.run_options["guard_mode"] for e in envelopes] == ["C0", "C3"]
    threads = {messages_from_payload(e.payload)[0].provider_thread_id for e in envelopes}
    assert len(threads) == 2  # never the same thread
    ids = [r["demo_request_id"] for r in response.json()["requests"]]
    assert len({UUID(i) for i in ids}) == 2


async def test_a_burst_sends_its_size() -> None:
    response, publisher = await _post("/v1/demo/scenarios/S13/run")

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert publisher.publish.await_count == 20


async def test_a_thread_scenario_sends_its_earlier_messages() -> None:
    _, publisher = await _post("/v1/demo/scenarios/S06/run")

    (envelope,) = [call.kwargs["envelope"] for call in publisher.publish.await_args_list]
    assert len(messages_from_payload(envelope.payload)) == 6


async def test_the_model_switch_overrides_the_profile() -> None:
    response, publisher = await _post("/v1/demo/scenarios/S03/run", {"model_profile": "live"})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    publisher.publish.assert_not_awaited()  # live is not configured: nothing published


@pytest.mark.parametrize(
    ("scenario_id", "code"),
    [("S14", status.HTTP_422_UNPROCESSABLE_CONTENT), ("S99", status.HTTP_404_NOT_FOUND)],
)
async def test_a_scenario_without_an_email_or_unknown_is_refused(
    scenario_id: str, code: int
) -> None:
    response, publisher = await _post(f"/v1/demo/scenarios/{scenario_id}/run")

    assert response.status_code == code
    publisher.publish.assert_not_awaited()
