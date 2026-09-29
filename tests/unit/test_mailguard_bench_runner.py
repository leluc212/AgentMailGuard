"""Benchmark run loop: per-case rows, resume, HTTP 429 back-off, error rows (task 7.19).

No mailguard import and no network: the case executor is a scripted double.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from evaluation.mailguard_bench.case_adapter import EvalCase
from evaluation.mailguard_bench.resilience import (
    BackoffPolicy,
    RateLimitedError,
    is_rate_limited,
    redact,
)
from evaluation.mailguard_bench.results import RESULT_SCHEMA, ResultStore
from evaluation.mailguard_bench.runner import filter_cases, run_cases
from packages.llm.protocol import LLMResponseError

OK: dict[str, Any] = {"blocked": False, "guard_errors": []}


def _case(case_id: str, kind: str = "attack") -> EvalCase:
    return EvalCase.from_dict(
        {
            "case_id": case_id,
            "kind": kind,
            "source": "llmail_inject",
            "technique": "llmail:adaptive",
            "vector": "email",
            "email": {"sender_email": "x@partner.example", "subject": "s", "body_text": "body"},
            "meta": {"scenario": "level2v"},
        }
    )


def _429() -> LLMResponseError:
    return LLMResponseError('LLM request failed with status 429: {"status": "RESOURCE_EXHAUSTED"}')


class Scripted:
    """Returns or raises the next scripted outcome for each case id."""

    def __init__(self, outcomes: dict[str, list[Any]]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    async def __call__(self, case: EvalCase) -> dict[str, Any]:
        self.calls.append(case.case_id)
        outcome = self.outcomes[case.case_id].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return dict(outcome)


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


async def test_each_case_is_written_as_one_row(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "rag-email__C3.jsonl")
    summary = await run_cases(
        [_case("a"), _case("b", "benign")],
        Scripted({"a": [OK], "b": [OK]}),
        store,
        config_name="C3",
        run_id="r1",
        sleep=Sleeps(),
    )
    rows = _rows(store.path)
    assert [r["case_id"] for r in rows] == ["a", "b"]
    assert {r["status"] for r in rows} == {"ok"}
    assert rows[0]["schema"] == RESULT_SCHEMA
    assert rows[0]["config"] == "C3" and rows[0]["run_id"] == "r1"
    assert rows[0]["scenario"] == "level2v"
    assert rows[1]["kind"] == "benign"
    assert rows[0]["result"] == OK
    assert (summary.ok, summary.error, summary.skipped) == (2, 0, 0)


async def test_resume_skips_case_ids_already_recorded(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "rag-email__C0.jsonl")
    await run_cases([_case("a")], Scripted({"a": [OK]}), store, config_name="C0", run_id="r")
    executor = Scripted({"b": [OK]})

    summary = await run_cases(
        [_case("a"), _case("b")], executor, store, config_name="C0", run_id="r"
    )

    assert executor.calls == ["b"]
    assert summary.skipped == 1
    assert [r["case_id"] for r in _rows(store.path)] == ["a", "b"]


async def test_http_429_backs_off_then_succeeds(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "r.jsonl")
    sleeps = Sleeps()
    await run_cases(
        [_case("a")],
        Scripted({"a": [_429(), _429(), OK]}),
        store,
        config_name="C3",
        run_id="r",
        policy=BackoffPolicy(max_attempts=6, base_s=2.0, cap_s=60.0),
        sleep=sleeps,
    )
    (row,) = _rows(store.path)
    assert row["status"] == "ok"
    assert row["attempts"] == 3
    assert sleeps.delays == [2.0, 4.0]


async def test_guard_side_rate_limit_is_retried_too(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "r.jsonl")
    await run_cases(
        [_case("a")],
        Scripted({"a": [RateLimitedError("guard LLM stage hit HTTP 429"), OK]}),
        store,
        config_name="C3",
        run_id="r",
        sleep=Sleeps(),
    )
    assert _rows(store.path)[0]["status"] == "ok"


async def test_exhausted_429_is_an_error_row_and_never_defended(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "r.jsonl")
    summary = await run_cases(
        [_case("a")],
        Scripted({"a": [_429(), _429(), _429()]}),
        store,
        config_name="C3",
        run_id="r",
        policy=BackoffPolicy(max_attempts=3, base_s=1.0, cap_s=1.0),
        sleep=Sleeps(),
    )
    (row,) = _rows(store.path)
    assert row["status"] == "error"
    assert row["error"]["kind"] == "rate_limited"
    assert row["attempts"] == 3
    assert row["result"] is None
    assert (summary.ok, summary.error) == (0, 1)


async def test_other_failures_are_error_rows_with_the_secret_redacted(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "r.jsonl")
    sleeps = Sleeps()
    await run_cases(
        [_case("a")],
        Scripted({"a": [LLMResponseError("LLM request failed with status 400: key=sk-SECRET")]}),
        store,
        config_name="C3",
        run_id="r",
        sleep=sleeps,
        secrets=["sk-SECRET"],
    )
    (row,) = _rows(store.path)
    assert row["status"] == "error"
    assert row["error"]["kind"] == "LLMResponseError"
    assert "sk-SECRET" not in row["error"]["message"]
    assert sleeps.delays == []


async def test_a_guard_layer_error_makes_the_row_an_error(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "r.jsonl")
    payload = {"blocked": True, "guard_errors": ["guard_llm: LLMTimeoutError: timed out"]}
    await run_cases([_case("a")], Scripted({"a": [payload]}), store, config_name="C3", run_id="r")
    (row,) = _rows(store.path)
    assert row["status"] == "error"
    assert row["error"]["kind"] == "guard_layer_error"
    assert row["result"]["blocked"] is True  # kept for diagnosis, never scored


async def test_retry_errors_reruns_only_error_rows_and_latest_row_wins(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "r.jsonl")
    await run_cases(
        [_case("a"), _case("b")],
        Scripted({"a": [OK], "b": [LLMResponseError("boom")]}),
        store,
        config_name="C3",
        run_id="r",
    )
    executor = Scripted({"b": [OK]})

    await run_cases(
        [_case("a"), _case("b")],
        executor,
        store,
        config_name="C3",
        run_id="r",
        retry_errors=True,
    )

    assert executor.calls == ["b"]
    assert store.latest_records()["b"]["status"] == "ok"
    assert len(_rows(store.path)) == 3


def test_store_skips_a_torn_line_and_appends_on_a_fresh_line(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    path.write_text(
        json.dumps({"case_id": "a", "status": "ok"}) + '\n{"case_id": "b", "st', "utf-8"
    )
    store = ResultStore(path)
    assert set(store.latest_records()) == {"a"}

    store.append({"case_id": "c", "status": "ok"})

    assert set(store.latest_records()) == {"a", "c"}
    assert store.skipped_lines == 1


async def test_concurrency_is_one_or_two(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        await run_cases(
            [],
            Scripted({}),
            ResultStore(tmp_path / "r.jsonl"),
            config_name="C3",
            run_id="r",
            concurrency=3,
        )


async def test_concurrent_cases_keep_their_own_guard_tallies(tmp_path: Path) -> None:
    """Two cases in flight on one shared CountingProvider (--concurrency 2).

    Case "a" hits a guard-LLM failure, then case "b" starts (begin_case) while "a" is still
    running. A per-provider counter would let b's start wipe a's error, and a would score
    as a clean defence. The error must stay with "a" and "b" must stay clean.
    """
    from evaluation.mailguard_bench.counting import CountingProvider

    a_failed = asyncio.Event()
    b_started = asyncio.Event()

    class Inner:
        model_name = "guard"

        async def generate(self, **kwargs: Any) -> Any:
            if kwargs["case"] == "a":
                raise LLMResponseError("OpenAI HTTP 500: upstream")
            return type("R", (), {"input_tokens": 7, "output_tokens": 3})()

    shared = CountingProvider(Inner())

    async def execute(case: EvalCase) -> dict[str, Any]:
        shared.begin_case()
        if case.case_id == "a":
            with pytest.raises(LLMResponseError):
                await shared.generate(case="a")
            a_failed.set()
            await b_started.wait()  # b begins and calls while a is still in flight
        else:
            await a_failed.wait()
            shared.begin_case()  # a second reset in b's context must not reach a
            b_started.set()
            await shared.generate(case="b")
        calls = shared.snapshot()
        return {"blocked": False, "guard_errors": calls["errors"], "guard_llm": calls}

    store = ResultStore(tmp_path / "r.jsonl")
    await run_cases(
        [_case("a"), _case("b")], execute, store, config_name="C3", run_id="r", concurrency=2
    )

    rows = store.latest_records()
    assert rows["a"]["status"] == "error"
    assert rows["a"]["error"]["kind"] == "guard_layer_error"
    assert rows["a"]["result"]["guard_llm"]["calls"] == 1
    assert rows["b"]["status"] == "ok"
    assert rows["b"]["result"]["guard_llm"] == {
        "model": "guard",
        "calls": 1,
        "input_tokens": 7,
        "output_tokens": 3,
        "errors": [],
    }


def test_rate_limit_detection_follows_the_cause_chain() -> None:
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    status = httpx.HTTPStatusError(
        "too many", request=request, response=httpx.Response(429, request=request)
    )
    wrapped = LLMResponseError("LLM request failed")
    wrapped.__cause__ = status
    assert is_rate_limited(wrapped)
    assert is_rate_limited(LLMResponseError("OpenAI HTTP 429: quota"))
    assert is_rate_limited(RateLimitedError("x"))
    assert not is_rate_limited(LLMResponseError("LLM request failed with status 500: 4290 ms"))


def test_backoff_doubles_and_caps() -> None:
    policy = BackoffPolicy(max_attempts=8, base_s=2.0, cap_s=30.0)
    assert [policy.delay(n) for n in range(1, 7)] == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    with pytest.raises(ValueError):
        BackoffPolicy(max_attempts=0)


def test_redact_masks_every_secret() -> None:
    assert redact("a sk-1 b sk-2", ["sk-1", None, "sk-2"]) == "a *** b ***"


def test_filter_cases_keeps_id_order_and_rejects_unknown_ids() -> None:
    cases = [_case("a"), _case("b"), _case("c")]
    assert [c.case_id for c in filter_cases(cases, case_ids=["c", "a"], limit=None)] == ["c", "a"]
    assert [c.case_id for c in filter_cases(cases, case_ids=None, limit=2)] == ["a", "b"]
    with pytest.raises(ValueError, match="not in the case file"):
        filter_cases(cases, case_ids=["zz"], limit=None)


def _case_set(sha: str) -> Any:
    from evaluation.mailguard_bench.cases import LoadedCaseSet

    sets = {
        "llmail_attack": ["a1", "a2"],
        "llmail_benign": ["b1"],
        "rag_attack": ["r1"],
        "ablation_attack": ["a2"],
    }
    cases = {cid: {"case_id": cid} for cid in ("a1", "a2", "b1", "r1")}
    return LoadedCaseSet(
        manifest={"seed": 20260930, "cases_sha256": sha, "sets": sets}, cases=cases
    )


def test_config_case_ids_full_run_and_ablation() -> None:
    from evaluation.mailguard_bench.runner import config_case_ids

    manifest = _case_set("s" * 64).manifest
    assert config_case_ids(manifest, "C0") == ["a1", "a2", "b1", "r1"]
    assert config_case_ids(manifest, "C3") == config_case_ids(manifest, "C0")
    assert config_case_ids(manifest, "C0T") == config_case_ids(manifest, "C0")
    assert config_case_ids(manifest, "C1") == ["a2", "b1"]
    assert config_case_ids(manifest, "C2") == ["a2", "b1"]


def test_run_folder_is_pinned_to_one_case_set(tmp_path: Path) -> None:
    from evaluation.mailguard_bench.cases import CaseManifestError
    from evaluation.mailguard_bench.runner import snapshot_case_set

    run = tmp_path / "run1"
    snapshot_case_set(_case_set("a" * 64), run)
    snapshot_case_set(_case_set("a" * 64), run)  # C3 after C0 on the same cases: accepted
    written = json.loads((run / "case_manifest.json").read_text(encoding="utf-8"))
    assert written["llmail_attack_ids"] == ["a1", "a2"]
    assert written["benign_ids"] == ["b1"]
    assert written["ablation_attack_ids"] == ["a2"]
    assert len((run / "cases.jsonl").read_text(encoding="utf-8").splitlines()) == 4
    with pytest.raises(CaseManifestError, match="another case set"):
        snapshot_case_set(_case_set("b" * 64), run)


def test_resume_refuses_other_settings_and_keeps_the_history(tmp_path: Path) -> None:
    from evaluation.mailguard_bench.runner import (
        RunSettingsMismatchError,
        check_resume,
        settings_fingerprint,
    )

    meta = {
        "preset": "C3",
        "rag_email_commit": "a" * 40,
        "generation_model": "gemma-4-26b-a4b-it",
        "generation": {"provider": "openai", "timeout_s": 60.0},
        "guard_models": "gemma-4-26b-a4b-it",
    }
    first = settings_fingerprint(meta)
    meta_file = tmp_path / "C3.meta.json"
    assert check_resume(meta_file, first) == []  # a fresh config starts with no history
    history = [{"started_at": "t1", "summary": {"ok": 5}}]
    meta_file.write_text(json.dumps({"fingerprint": first, "invocations": history}), "utf-8")

    assert check_resume(meta_file, first) == history  # same settings: resume, history kept

    for changed in (
        {"rag_email_commit": "b" * 40},
        {"generation_model": "gemma-3-27b-it"},
        {"generation": {"provider": "openai", "timeout_s": 30.0}},
        {"guard_models": "fake"},
    ):
        with pytest.raises(RunSettingsMismatchError, match=next(iter(changed))):
            check_resume(meta_file, settings_fingerprint({**meta, **changed}))
    meta_file.write_text(json.dumps({"invocations": history}), "utf-8")  # pre-v2 meta
    with pytest.raises(RunSettingsMismatchError, match="no settings fingerprint"):
        check_resume(meta_file, first)


def test_resume_accepts_its_own_json_round_tripped_fingerprint(tmp_path: Path) -> None:
    """A guarded config's live_layers holds tuples; JSON stores them as lists.

    Live bug 2026-09-29: every resume of C0T/C3 was refused ("live_layers changed")
    because () != [] after the meta file round trip.
    """
    from evaluation.mailguard_bench.runner import check_resume, settings_fingerprint

    meta = {
        "preset": "C3",
        "live_layers": {"preset": "C3", "active_layers": ("L1", "L2", "L3"), "l3b_llm": None},
    }
    first = settings_fingerprint(meta)
    history = [{"started_at": "t1", "summary": {"ok": 1}}]
    meta_file = tmp_path / "C3.meta.json"
    meta_file.write_text(json.dumps({"fingerprint": first, "invocations": history}), "utf-8")

    assert check_resume(meta_file, settings_fingerprint(meta)) == history


def test_c0_runs_the_native_executor_and_the_other_configs_need_a_guard() -> None:
    """Owner decision 2026-09-29: C0 = rag-email's generate_draft, C0T/C1/C2/C3 = the guard."""
    from evaluation.mailguard_bench.native_reply import NativeCaseExecutor
    from evaluation.mailguard_bench.runner import BENCH_CONFIGS, prepared_case_executor
    from packages.llm import AgentProfileRegistry, FakeLLMProvider, SinglePassGenerator

    assert BENCH_CONFIGS == ("C0", "C0T", "C1", "C2", "C3")
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(default_response={}),
        profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
    )
    native = prepared_case_executor("C0", generator=generator, guard=None)
    assert isinstance(native, NativeCaseExecutor)
    assert native.generator is generator
    for config in ("C0T", "C1", "C2", "C3"):
        with pytest.raises(ValueError, match="needs a guard"):
            prepared_case_executor(config, generator=generator, guard=None)
    with pytest.raises(ValueError, match="no guard"):
        prepared_case_executor("C0", generator=generator, guard=object())


def test_native_guard_facts_record_the_same_environment_as_a_guarded_run(
    tmp_path: Path,
) -> None:
    """C0 meta carries the pinned commit and L1 hash so Task 5 can pair it with C3."""
    from evaluation.mailguard_bench.guard_env import GuardPaths, sha256_file
    from evaluation.mailguard_bench.runner import native_guard_facts

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    paths = GuardPaths(root=tmp_path / "not-git", commit="c" * 40, artifacts=artifacts)
    paths.root.mkdir()
    facts = native_guard_facts(paths)
    assert facts["preset"] is None and facts["config"] == "C0"
    assert facts["active_layers"] == [] and facts["missing_live_stages"] == []
    assert facts["l1_model_sha256"] is None
    assert facts["mailguard_commit"] is None  # outside git: never a guessed SHA

    paths.l1_model.write_bytes(b"classifier")
    assert native_guard_facts(paths)["l1_model_sha256"] == sha256_file(paths.l1_model)


def test_live_rows_have_their_own_schema_and_v1_keeps_its_own() -> None:
    """Task 7.20: the services-v2 rows are ``.v3``; a v1 reader never mistakes one for v1."""
    from evaluation.mailguard_bench.results import RESULT_SCHEMA_V3

    assert RESULT_SCHEMA_V3 == "mailguard-bench-result.v3"
    assert RESULT_SCHEMA == "mailguard-bench-result.v1"


async def test_run_cases_stamps_every_row_kind_with_the_schema_it_is_given(
    tmp_path: Path,
) -> None:
    from evaluation.mailguard_bench.results import RESULT_SCHEMA_V3

    store = ResultStore(tmp_path / "r.jsonl")
    await run_cases(
        [_case("ok"), _case("boom"), _case("guard")],
        Scripted(
            {
                "ok": [OK],
                "boom": [LLMResponseError("LLM request failed with status 400")],
                "guard": [{"guard_errors": ["l1: timed out"]}],
            }
        ),
        store,
        config_name="C0",
        run_id="r",
        schema=RESULT_SCHEMA_V3,
    )
    rows = _rows(store.path)
    assert [r["status"] for r in rows] == ["ok", "error", "error"]
    assert {r["schema"] for r in rows} == {RESULT_SCHEMA_V3}


def test_build_record_defaults_to_the_v1_schema() -> None:
    from evaluation.mailguard_bench.runner import build_record

    row = build_record(_case("a"), config_name="C3", run_id="r", status="ok", attempts=1)
    assert row["schema"] == RESULT_SCHEMA


def test_the_fingerprint_takes_extra_keys_and_v1_keeps_its_own() -> None:
    from evaluation.mailguard_bench.runner import FINGERPRINT_KEYS, settings_fingerprint

    meta = {"preset": "C3", "transport": "services-v2"}
    assert "transport" not in settings_fingerprint(meta)
    wider = settings_fingerprint(meta, keys=(*FINGERPRINT_KEYS, "transport"))
    assert wider["transport"] == "services-v2"
    assert set(FINGERPRINT_KEYS) < set(wider)


async def test_one_advisory_lock_key_names_a_run_and_config_for_both_runners() -> None:
    from evaluation.mailguard_bench.runner import run_lock_key, try_acquire_run_lock

    class Conn:
        def __init__(self, granted: bool) -> None:
            self.granted = granted
            self.calls: list[tuple[str, str]] = []

        async def fetchval(self, query: str, key: str) -> bool:
            self.calls.append((query, key))
            return self.granted

    assert run_lock_key("2026-09-29-qwen-live", "C3") == "mailguard-bench 2026-09-29-qwen-live/C3"
    free, held = Conn(True), Conn(False)
    assert await try_acquire_run_lock(free, "r", "C0") is True
    assert await try_acquire_run_lock(held, "r", "C0") is False
    assert free.calls == [("SELECT pg_try_advisory_lock(hashtext($1))", "mailguard-bench r/C0")]


def test_generation_meta_maps_every_tier_the_way_the_llm_factory_does() -> None:
    """The v1 and the live runner record the same ``generation`` blocks (one implementation)."""
    from evaluation.mailguard_bench.runner import generation_meta
    from packages.core.settings import LLMTiersSettings

    tiers = LLMTiersSettings(
        provider="openai",
        openai_base_url="http://localhost:11434/v1",
        fast_model="fast-m",
        strong_model="strong-m",
        fallback_model="fallback-m",
        timeout_s=45.0,
    )
    assert generation_meta(tiers) == {
        "generation_model": "fast-m",  # the routine tier is what the reply profile uses
        "generation": {
            "provider": "openai",
            "base_url": "http://localhost:11434/v1",
            "model": "fast-m",
            "model_map": {
                "fast": "fast-m",
                "routine": "fast-m",
                "strong": "strong-m",
                "fallback": "fallback-m",
            },
            "force_single_tier": False,
            "timeout_s": 45.0,
        },
    }

    single = generation_meta(tiers.model_copy(update={"force_single_tier": True}))
    assert single["generation_model"] == "strong-m"  # every tier is the strong model
    assert set(single["generation"]["model_map"].values()) == {"strong-m"}
