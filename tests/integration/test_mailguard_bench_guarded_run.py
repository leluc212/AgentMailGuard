"""Whole benchmark path on Postgres + fake providers: org, KB, context, guard, one call.

Skipped when mailguard is not importable (CI). Run: make mailguard-bench-test.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import asyncpg
import pytest

pytest.importorskip("mailguard")

from evaluation.mailguard_bench.case_adapter import (  # noqa: E402
    EVAL_ORG_PREFIX,
    EvalCase,
    EvalHost,
)
from evaluation.mailguard_bench.guard_build import build_guard  # noqa: E402
from evaluation.mailguard_bench.results import ResultStore  # noqa: E402
from evaluation.mailguard_bench.runner import (  # noqa: E402
    HostCaseExecutor,
    prepared_case_executor,
    run_cases,
)
from packages.core.settings import AppSettings  # noqa: E402
from packages.db.connection import create_pool_from_settings  # noqa: E402
from packages.knowledge.embedder import FakeEmbedder  # noqa: E402
from packages.llm import AgentProfileRegistry, FakeLLMProvider, SinglePassGenerator  # noqa: E402
from tests.unit.test_mailguard_bench_case_adapter import PRAG  # noqa: E402
from tests.unit.test_mailguard_bench_guarded_reply import REPLY, guard_fake  # noqa: E402


@pytest.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool[Any], None]:
    pool = await create_pool_from_settings(AppSettings().database)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.mark.parametrize(
    ("config", "scheme"),
    [
        ("C0", "v1"),
        ("C0T", "v1"),
        ("C3", "v1"),
        # scheme v2 (ADR-0012 decision 11): the guard is built from explicit layer flags
        ("C0T", "v2"),
        ("C3", "v2"),  # channel isolation + L5
        ("C4", "v2"),  # L3b + L5
        ("C6", "v2"),  # L5 alone
        ("C7", "v2"),
    ],
)
async def test_rag_case_runs_end_to_end_and_leaves_no_tenant_behind(
    db_pool: asyncpg.Pool[Any], tmp_path: Path, config: str, scheme: str
) -> None:
    registry = AgentProfileRegistry.from_yaml("config/agent_profiles.yaml")
    host = EvalHost.create(
        AppSettings(), pool=db_pool, embedder=FakeEmbedder(), profile_registry=registry
    )
    guard = None
    if config != "C0":  # C0 is rag-email's native path: no AgentMailGuard code runs
        guard = build_guard(
            config,
            model_name="fake",
            audit_log_path=tmp_path / "audit.jsonl",
            l1_model_path=tmp_path / "clf.joblib",
            scheme=scheme,
        )
        guard.guard_llm.inner = guard_fake()  # schema-valid guard answers (guarded_reply tests)
    fake = FakeLLMProvider(default_response=REPLY)
    executor = HostCaseExecutor(
        host=host,
        executor=prepared_case_executor(
            config,
            generator=SinglePassGenerator(llm_provider=fake, profile_registry=registry),
            guard=guard,
        ),
        label=f"it/{config}",
    )
    store = ResultStore(tmp_path / f"rag-email__{config}.jsonl")

    summary = await run_cases(
        [EvalCase.from_dict(PRAG)], executor, store, config_name=config, run_id="it"
    )

    row = store.latest_records()[PRAG["case_id"]]
    assert summary.error == 0, row["error"]
    assert row["status"] == "ok"
    assert row["result"]["host"]["poison_retrieved"] is True
    assert len(fake.recorded_calls) <= 1
    if config == "C0":
        # rag-email's own profile template, one rendered user message, no guard report.
        assert [m.role for m in fake.recorded_calls[0]["messages"]] == ["user"]
        assert row["result"]["prompt_mode"] == "native"
        assert row["result"]["report"] is None
        assert row["result"]["final_draft"] == {"action": "reply", "body": REPLY["draft"]}
    assert (
        await db_pool.fetchval(
            "SELECT count(*) FROM organization WHERE name LIKE $1", f"{EVAL_ORG_PREFIX} %"
        )
        == 0
    )
