"""AgentMailGuard benchmark runner hosted by rag-email (task 7.19; spec §4, §4b, §5; ADR-0010).

    make mailguard-bench RUN=<id> CONFIG=C0|C3|C1|C2 [LIMIT=n]

Owner-run live evaluation (real Gemini calls). It is never part of ``make ci`` (R24.5).
For each case it opens a throwaway organization, ingests the case KB, builds the real
ContextPackage, and runs AgentMailGuard's MailGuardPipeline around ONE
SinglePassGenerator call. Rows are appended per case, recorded case ids are skipped on
resume, HTTP 429 backs off, and a failure is an ``error`` row, never a defence.

Run it from the repo root as a module (``python -m``) so rag-email's ``services`` and
``evaluation`` packages win over AgentMailGuard's same-named ones on sys.path.
.env keys used: LLM__PROVIDER, LLM__OPENAI_BASE_URL, LLM__OPENAI_API_KEY (never printed),
LLM__FAST_MODEL, DATABASE__*, EMBEDDING__*, RETRIEVAL__*.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from evaluation.mailguard_bench.case_adapter import EvalCase
from evaluation.mailguard_bench.resilience import BackoffPolicy, is_rate_limited, redact
from evaluation.mailguard_bench.results import RESULT_SCHEMA, ResultStore

CaseExecutor = Callable[[EvalCase], Awaitable[dict[str, Any]]]
Sleep = Callable[[float], Awaitable[None]]
MAX_CONCURRENCY = 2  # spec §5: concurrency 1-2


@dataclass
class RunSummary:
    selected: int
    skipped: int = 0
    ok: int = 0
    error: int = 0


def build_record(
    case: EvalCase,
    *,
    config_name: str,
    run_id: str,
    status: str,
    attempts: int,
    error: dict[str, str] | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "run_id": run_id,
        "config": config_name,
        "case_id": case.case_id,
        "kind": case.kind,
        "source": case.source,
        "technique": case.technique,
        "vector": case.vector,
        "scenario": case.scenario,
        "status": status,
        "attempts": attempts,
        "error": error,
        "finished_at": datetime.now(UTC).isoformat(),
        "result": result,
    }


async def _run_one(
    case: EvalCase,
    execute: CaseExecutor,
    *,
    config_name: str,
    run_id: str,
    policy: BackoffPolicy,
    sleep: Sleep,
    secrets: Sequence[str | None],
) -> dict[str, Any]:
    attempt = 0
    while True:
        attempt += 1
        try:
            result = await execute(case)
        except Exception as exc:
            limited = is_rate_limited(exc)
            if limited and attempt < policy.max_attempts:
                await sleep(policy.delay(attempt))
                continue
            return build_record(
                case,
                config_name=config_name,
                run_id=run_id,
                status="error",
                attempts=attempt,
                error={
                    "kind": "rate_limited" if limited else type(exc).__name__,
                    "message": redact(str(exc), secrets)[:2000],
                },
            )
        guard_errors = [str(e) for e in result.get("guard_errors") or []]
        if guard_errors:
            return build_record(
                case,
                config_name=config_name,
                run_id=run_id,
                status="error",
                attempts=attempt,
                error={
                    "kind": "guard_layer_error",
                    "message": redact("; ".join(guard_errors), secrets)[:2000],
                },
                result=result,
            )
        return build_record(
            case,
            config_name=config_name,
            run_id=run_id,
            status="ok",
            attempts=attempt,
            result=result,
        )


async def run_cases(
    cases: Sequence[EvalCase],
    execute: CaseExecutor,
    store: ResultStore,
    *,
    config_name: str,
    run_id: str,
    policy: BackoffPolicy | None = None,
    retry_errors: bool = False,
    concurrency: int = 1,
    sleep: Sleep = asyncio.sleep,
    secrets: Sequence[str | None] = (),
    on_record: Callable[[dict[str, Any]], None] | None = None,
) -> RunSummary:
    """Run every case not yet recorded; append each row as soon as it exists.

    Args:
        retry_errors: Also re-run cases whose latest row is an ``error`` row.
        concurrency: 1 or 2 cases in flight (spec §5).

    Raises:
        ValueError: If concurrency is outside 1..2.
    """
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be 1 or 2 (spec §5), got {concurrency}")
    backoff = policy or BackoffPolicy()
    latest = store.latest_records()
    todo = [
        case
        for case in cases
        if case.case_id not in latest
        or (retry_errors and latest[case.case_id].get("status") == "error")
    ]
    summary = RunSummary(selected=len(cases), skipped=len(cases) - len(todo))
    gate = asyncio.Semaphore(concurrency)

    async def one(case: EvalCase) -> None:
        async with gate:
            record = await _run_one(
                case,
                execute,
                config_name=config_name,
                run_id=run_id,
                policy=backoff,
                sleep=sleep,
                secrets=secrets,
            )
            store.append(record)
            if record["status"] == "ok":
                summary.ok += 1
            else:
                summary.error += 1
            if on_record is not None:
                on_record(record)

    await asyncio.gather(*(one(case) for case in todo))
    return summary


def filter_cases(
    cases: Sequence[EvalCase], *, case_ids: Sequence[str] | None, limit: int | None
) -> list[EvalCase]:
    """Restrict to an id list (in its order) and/or the first ``limit`` cases.

    Raises:
        ValueError: If an id is not in the case file.
    """
    selected = list(cases)
    if case_ids is not None:
        by_id = {case.case_id: case for case in cases}
        missing = [case_id for case_id in case_ids if case_id not in by_id]
        if missing:
            raise ValueError(f"{len(missing)} case ids not in the case file, first {missing[0]}")
        selected = [by_id[case_id] for case_id in case_ids]
    return selected[:limit] if limit is not None else selected
