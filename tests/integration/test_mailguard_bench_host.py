"""Benchmark host on Postgres: real KB ingestion, real retrieval, org cleanup (task 7.19).

Runs in rag_email_test (tests/integration/conftest.py) on the FakeEmbedder; no LLM call.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID

import asyncpg
import pytest

from evaluation.mailguard_bench.case_adapter import (
    EVAL_ORG_PREFIX,
    EvalCase,
    EvalHost,
    eval_organization,
    purge_stale_eval_orgs,
)
from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from packages.knowledge.embedder import FakeEmbedder
from packages.llm.profile import AgentProfileRegistry
from tests.unit.test_mailguard_bench_case_adapter import LLMAIL, PRAG


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool[Any], None]:
    pool = await create_pool_from_settings(AppSettings().database)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def host(db_pool: asyncpg.Pool[Any]) -> EvalHost:
    return EvalHost.create(
        AppSettings(),
        pool=db_pool,
        embedder=FakeEmbedder(),
        profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
    )


async def _org_rows(pool: asyncpg.Pool[Any], org_id: UUID) -> tuple[int, int, int]:
    orgs = await pool.fetchval("SELECT count(*) FROM organization WHERE id = $1", org_id)
    docs = await pool.fetchval(
        "SELECT count(*) FROM knowledge_document WHERE organization_id = $1", org_id
    )
    embeddings = await pool.fetchval(
        "SELECT count(*) FROM embedding_record WHERE organization_id = $1", org_id
    )
    return int(orgs), int(docs), int(embeddings)


async def test_poisoned_kb_docs_are_ingested_and_reach_the_context_through_retrieval(
    host: EvalHost, db_pool: asyncpg.Pool[Any]
) -> None:
    case = EvalCase.from_dict(PRAG)
    async with eval_organization(db_pool, label="it rag") as org_id:
        prepared = await host.prepare(case, organization_id=org_id)
        orgs, docs, embeddings = await _org_rows(db_pool, org_id)
        assert (orgs, docs) == (1, 2)
        assert embeddings >= 2
        statuses = await db_pool.fetch(
            "SELECT status, category FROM knowledge_document WHERE organization_id = $1", org_id
        )
        assert {(r["status"], r["category"]) for r in statuses} == {("active", "support")}

    assert prepared.poison_ingested is True
    assert prepared.context.retrieved_chunks, "retrieval returned nothing"
    assert prepared.poison_retrieved is True
    assert "prag-nq-t1-0" in {r.case_chunk_id for r in prepared.retrieved}
    assert [r.rank for r in prepared.retrieved] == list(range(1, len(prepared.retrieved) + 1))
    assert prepared.context.business_data is None
    assert prepared.context.current_message.body_text == PRAG["email"]["body_text"]
    assert "moby dick" in prepared.retrieval_query.lower()
    assert prepared.diagnostics()["kb_docs_ingested"] == 2
    # The throwaway org and everything under it are gone.
    assert await _org_rows(db_pool, org_id) == (0, 0, 0)


async def test_email_only_case_has_no_kb_and_no_retrieved_chunks(
    host: EvalHost, db_pool: asyncpg.Pool[Any]
) -> None:
    async with eval_organization(db_pool, label="it email") as org_id:
        prepared = await host.prepare(EvalCase.from_dict(LLMAIL), organization_id=org_id)
    assert prepared.ingested == {}
    assert prepared.retrieved == ()
    assert prepared.context.retrieved_chunks == []
    assert prepared.poison_retrieved is False
    assert prepared.classification.category == "support"


async def test_a_case_never_retrieves_another_cases_documents(
    host: EvalHost, db_pool: asyncpg.Pool[Any]
) -> None:
    other = EvalCase.from_dict({**PRAG, "case_id": "attack-prag-nq-other"})
    async with eval_organization(db_pool, label="it a") as org_a:
        await host.prepare(other, organization_id=org_a)
        async with eval_organization(db_pool, label="it b") as org_b:
            prepared = await host.prepare(EvalCase.from_dict(LLMAIL), organization_id=org_b)
    assert prepared.retrieved == ()


async def test_org_is_deleted_even_when_the_case_fails(db_pool: asyncpg.Pool[Any]) -> None:
    captured: list[UUID] = []
    with pytest.raises(RuntimeError, match="boom"):
        async with eval_organization(db_pool, label="it fail") as org_id:
            captured.append(org_id)
            raise RuntimeError("boom")
    assert await _org_rows(db_pool, captured[0]) == (0, 0, 0)


async def test_purge_removes_only_benchmark_orgs(db_pool: asyncpg.Pool[Any]) -> None:
    await db_pool.execute(
        "INSERT INTO organization (id, name) VALUES (gen_random_uuid(), $1)",
        f"{EVAL_ORG_PREFIX} crashed_run/C3 attack-x",
    )
    keep = await db_pool.fetchval(
        "INSERT INTO organization (id, name) VALUES (gen_random_uuid(), 'Demo Tenant') RETURNING id"
    )
    assert await purge_stale_eval_orgs(db_pool, scope="crashed_run/C3") == 1
    assert await db_pool.fetchval("SELECT count(*) FROM organization WHERE id = $1", keep) == 1
    await db_pool.execute("DELETE FROM organization WHERE id = $1", keep)


async def test_purge_leaves_another_configs_live_org_alone(db_pool: asyncpg.Pool[Any]) -> None:
    # Two terminals: C0 and C3 of the same RUN. C3's start-up purge must not
    # cascade-delete the knowledge base of the case C0 has in flight. The `_` in
    # the run name must not act as a LIKE wildcard either ("fullXrun" is not "full_run").
    async with eval_organization(db_pool, label="full_run/C0 attack-live") as live_org:
        other = await db_pool.fetchval(
            "INSERT INTO organization (id, name) VALUES (gen_random_uuid(), $1) RETURNING id",
            f"{EVAL_ORG_PREFIX} fullXrun/C3 attack-y",
        )
        assert await purge_stale_eval_orgs(db_pool, scope="full_run/C3") == 0
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM organization WHERE id = ANY($1::uuid[])", [live_org, other]
            )
            == 2
        )
        await db_pool.execute("DELETE FROM organization WHERE id = $1", other)
