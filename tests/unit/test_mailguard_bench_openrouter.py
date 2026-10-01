"""The OpenRouter route in the benchmark harness (work package R4; ADR-0012 decision 9).

A run that pins one provider must record which provider served every call, in rag-email's
generation and in the guard's judges, and must never score a call another provider served. These
tests drive the harness with stub providers and an ``httpx.MockTransport``: nothing calls the
network (R24.5), and none needs AgentMailGuard.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from evaluation.mailguard_bench.case_adapter import (
    EvalCase,
    PreparedCase,
    classification_for,
    to_normalized_message,
)
from evaluation.mailguard_bench.counting import CountingProvider
from evaluation.mailguard_bench.guarded_reply import generation_summary
from evaluation.mailguard_bench.model_profiles import get_profile, profile_env
from evaluation.mailguard_bench.native_reply import NativeCaseExecutor
from evaluation.mailguard_bench.resilience import BackoffPolicy, is_rate_limited
from evaluation.mailguard_bench.results import ResultStore
from evaluation.mailguard_bench.route import (
    STOP_AFTER,
    RouteBreaker,
    expected_guard_route,
    guard_pin_problem,
    pin_problem,
    provenance_summary,
    route_failure,
    route_meta,
    stop_message,
)
from evaluation.mailguard_bench.runner import generation_meta, run_cases
from packages.core.settings import AppSettings, LLMTiersSettings, ProviderRouting
from packages.domain.entities import ContextPackage
from packages.llm import AgentProfileRegistry, SinglePassGenerator
from packages.llm.client import HttpLLMProvider
from packages.llm.protocol import (
    CallProvenance,
    ChatMessage,
    LLMResult,
    ModelTier,
)

LLAMA = "meta-llama/llama-3.1-8b-instruct"
PIN = ProviderRouting(order=["coreweave"], quantizations=["bf16"])
REPLY: dict[str, Any] = {
    "action": "reply",
    "draft": "Thanks for reaching out, please reinstall the driver from the vendor page.",
    "confidence": 0.8,
    "knowledge_chunks": [],
    "thread_summary_updated": False,
    "model_tier": "routine",
}


def prov(provider: str | None = "CoreWeave", attempt: int | None = 1, **kw: Any) -> CallProvenance:
    return CallProvenance(
        requested_model=LLAMA,
        served_provider=provider,
        attempt=attempt,
        generation_id=kw.pop("generation_id", "gen-1"),
        prompt_tokens=kw.pop("prompt_tokens", 10),
        completion_tokens=kw.pop("completion_tokens", 4),
        cost=kw.pop("cost", 0.000005),
        finish_reason="stop",
        **kw,
    )


class StubProvider:
    """A provider that answers a queue of contents, each with a provenance."""

    def __init__(self, contents: Sequence[dict[str, Any]], provenance: Sequence[CallProvenance]):
        self._contents = list(contents)
        self._provenance = list(provenance)
        self.calls = 0

    async def generate(
        self,
        *,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        tier: ModelTier = ModelTier.FAST,
        max_tokens: int = 1000,
        temperature: float = 0.0,
        **params: Any,
    ) -> LLMResult:
        index = min(self.calls, len(self._contents) - 1)
        self.calls += 1
        return LLMResult(
            content=self._contents[index],
            model=LLAMA,
            tier=tier,
            input_tokens=10,
            output_tokens=4,
            latency_ms=5,
            provenance=self._provenance[min(index, len(self._provenance) - 1)],
        )


def prepared(body: str = "My printer driver fails to install.") -> PreparedCase:
    case = EvalCase.from_dict(
        {
            "case_id": "o-1",
            "kind": "benign",
            "category": "support",
            "email": {"sender_email": "x@partner.example", "subject": "Help", "body_text": body},
        }
    )
    org = uuid4()
    message = to_normalized_message(case, organization_id=org, received_at=datetime.now(UTC))
    return PreparedCase(
        case=case,
        organization_id=org,
        message=message,
        classification=classification_for(case),
        context=ContextPackage(
            agent_instructions="You are an enterprise support assistant.",
            category_instructions="Address technical questions.",
            current_message=message,
        ),
        retrieval_query=body,
        ingested={},
        retrieved=(),
        context_ms=0,
    )


def generator(provider: Any) -> SinglePassGenerator:
    return SinglePassGenerator(
        llm_provider=provider,
        profile_registry=AgentProfileRegistry.from_yaml("config/agent_profiles.yaml"),
    )


# --- the generation call's provenance in the row -----------------------------------------


async def test_the_generation_result_carries_the_provenance_of_its_call() -> None:
    result = await generator(StubProvider([REPLY], [prov()])).generate_draft(prepared().context)

    assert [p.served_provider for p in result.provenance] == ["CoreWeave"]
    assert generation_summary(result)["provenance"] == [prov().to_dict()]


async def test_a_repaired_generation_records_both_calls() -> None:
    bad = {"draft": "no action or confidence"}
    stub = StubProvider([bad, REPLY], [prov(generation_id="g-first"), prov(generation_id="g-2")])

    result = await generator(stub).generate_draft(prepared().context)

    assert result.repair_attempts == 1
    assert [p.generation_id for p in result.provenance] == ["g-first", "g-2"]
    assert generation_summary(result)["calls"] == 2


async def test_a_generation_without_provenance_keeps_the_v1_row_shape() -> None:
    plain = StubProvider([REPLY], [None])  # type: ignore[list-item]
    result = await generator(plain).generate_draft(prepared().context)

    assert result.provenance == ()
    assert "provenance" not in generation_summary(result)
    assert "provenance" not in generation_summary(None)


async def test_a_c0_row_records_the_served_provider() -> None:
    executor = NativeCaseExecutor(generator=generator(StubProvider([REPLY], [prov()])))

    execution = await executor.execute(prepared())

    assert execution.record["generation"]["provenance"][0]["served_provider"] == "CoreWeave"
    assert execution.guard_errors == ()


async def test_a_call_another_provider_served_fails_the_case_through_the_real_client() -> None:
    # The full path with no stub provider: HTTP response -> client check -> generator -> C0.
    served_by_other = {
        "id": "gen-x",
        "model": LLAMA,
        "choices": [{"message": {"content": json.dumps(REPLY)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 3},
        "openrouter_metadata": {"summary": "available=1, selected=Groq", "attempt": 1},
    }
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=served_by_other))
    )
    llm = HttpLLMProvider(
        base_url="https://openrouter.ai/api/v1",
        api_key="k",
        model_map=dict.fromkeys(ModelTier, LLAMA),
        provider_routing=PIN,
        response_metadata=True,
        client=client,
    )

    with pytest.raises(Exception, match="provider_mismatch") as caught:
        await NativeCaseExecutor(generator=generator(llm)).execute(prepared())

    assert type(caught.value).__name__ == "LLMProviderMismatchError"


# --- the guard's judges ------------------------------------------------------------------


class GuardStub:
    """What AgentMailGuard's OpenAIProvider looks like to rag-email: a pin and provenance."""

    def __init__(
        self,
        provenance: CallProvenance | None,
        routing: dict[str, Any] | None = None,
    ) -> None:
        self.model = LLAMA
        self.provider_routing = routing
        self._provenance = provenance

    async def generate(self, **kwargs: Any) -> Any:
        return LLMResult(
            content={"ok": True},
            model=LLAMA,
            tier=ModelTier.FAST,
            input_tokens=7,
            output_tokens=2,
            provenance=self._provenance,
        )


ROUTING_DICT = {
    "order": ["coreweave"],
    "allow_fallbacks": False,
    "require_parameters": True,
    "quantizations": ["bf16"],
}


async def test_every_guard_call_is_recorded_with_the_provider_that_served_it() -> None:
    counting = CountingProvider(GuardStub(prov(), ROUTING_DICT))
    counting.begin_case()

    await counting.generate(messages=[])
    await counting.generate(messages=[])
    snapshot = counting.snapshot()

    assert snapshot["calls"] == 2
    assert [p["served_provider"] for p in snapshot["provenance"]] == ["CoreWeave", "CoreWeave"]
    assert "route_violations" not in snapshot


async def test_a_guard_call_served_elsewhere_is_a_violation_not_an_exception() -> None:
    # Raising would only make the judge stage fall back, and a fallback row is scored: the
    # violation is recorded so the case becomes an error row instead.
    counting = CountingProvider(GuardStub(prov("DeepInfra"), ROUTING_DICT))
    counting.begin_case()

    result = await counting.generate(messages=[])
    snapshot = counting.snapshot()

    assert result.content == {"ok": True}  # the stage still gets its answer
    assert len(snapshot["route_violations"]) == 1
    assert snapshot["route_violations"][0].startswith("provider_mismatch:")
    assert "DeepInfra" in snapshot["route_violations"][0]


@pytest.mark.parametrize("provenance", [prov(attempt=2), prov(provider=None, attempt=None), None])
async def test_a_second_attempt_or_unreadable_provenance_is_a_violation(
    provenance: CallProvenance | None,
) -> None:
    counting = CountingProvider(GuardStub(provenance, ROUTING_DICT))
    counting.begin_case()

    await counting.generate(messages=[])

    assert len(counting.snapshot()["route_violations"]) == 1


async def test_an_unpinned_guard_provider_is_never_checked() -> None:
    counting = CountingProvider(GuardStub(prov("DeepInfra", attempt=2), routing=None))
    counting.begin_case()

    await counting.generate(messages=[])
    snapshot = counting.snapshot()

    assert "route_violations" not in snapshot
    assert snapshot["provenance"][0]["served_provider"] == "DeepInfra"


async def test_a_guard_provider_without_provenance_leaves_the_snapshot_as_v1() -> None:
    counting = CountingProvider(GuardStub(None, routing=None))
    counting.begin_case()

    await counting.generate(messages=[])

    assert set(counting.snapshot()) == {"model", "calls", "input_tokens", "output_tokens", "errors"}


# --- stopping the run ---------------------------------------------------------------------


def error_row(kind: str, message: str) -> dict[str, Any]:
    return {"status": "error", "error": {"kind": kind, "message": message}}


MISMATCH = error_row("LLMProviderMismatchError", "provider_mismatch: served by 'Groq'")
GUARD_MISMATCH = error_row("guard_layer_error", "provider_mismatch: served by 'DeepInfra'")
NO_PROVIDER = error_row("LLMResponseError", "LLM request failed with status 503: no provider")
NOT_FOUND = error_row("LLMResponseError", "LLM request failed with status 404: no endpoints")
BAD_GATEWAY = error_row("LLMResponseError", "LLM request failed with status 502: down")
NO_CREDIT_TEXT = "LLM request failed with status 402: " + json.dumps(
    {"error": {"metadata": {"limit_source": "openrouter_credits"}}}
)
IN_FLIGHT_TEXT = (
    "LLM request failed with status 402: "
    '{"error":{"metadata":{"limit_source":"openrouter_in_flight_budget"}}}'
)
NO_CREDIT = error_row("LLMResponseError", NO_CREDIT_TEXT)
IN_FLIGHT = error_row("LLMResponseError", IN_FLIGHT_TEXT)
OK = {"status": "ok"}


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (MISMATCH, "provider_mismatch"),
        (GUARD_MISMATCH, "provider_mismatch"),
        (NO_PROVIDER, "no_provider"),
        (NOT_FOUND, "no_provider"),
        (BAD_GATEWAY, "no_provider"),
        (NO_CREDIT, "no_credit"),
        (IN_FLIGHT, None),  # transient: retried with back-off, never a stop
        (error_row("LLMSchemaValidationError", "bad json"), None),
        (error_row("LLMResponseError", "LLM request failed with status 500: x"), None),
        (OK, None),
    ],
)
def test_route_failure_names_the_failures_that_should_stop_a_run(
    record: dict[str, Any], reason: str | None
) -> None:
    assert route_failure(record) == reason


def test_three_consecutive_route_failures_stop_the_run() -> None:
    breaker = RouteBreaker()
    assert STOP_AFTER == 3
    for record in (MISMATCH, GUARD_MISMATCH):
        breaker.observe(record)
        assert breaker.tripped is None
    breaker.observe(MISMATCH)
    assert breaker.tripped is not None
    assert "provider_mismatch" in breaker.tripped and "3" in breaker.tripped


def test_a_row_that_is_not_a_route_failure_resets_the_count() -> None:
    breaker = RouteBreaker()
    for record in (MISMATCH, MISMATCH, OK, MISMATCH, MISMATCH):
        breaker.observe(record)
    assert breaker.tripped is None
    breaker.observe(NO_PROVIDER)  # a different route failure still counts as one in a row
    assert breaker.tripped is not None


def test_no_credit_stops_at_once() -> None:
    breaker = RouteBreaker()
    breaker.observe(NO_CREDIT)
    assert breaker.tripped is not None and "credit" in breaker.tripped


def test_a_transient_in_flight_402_is_retried_like_a_rate_limit() -> None:
    from packages.llm.protocol import LLMResponseError

    transient = LLMResponseError(IN_FLIGHT_TEXT, status_code=402)
    permanent = LLMResponseError(NO_CREDIT_TEXT, status_code=402)
    assert is_rate_limited(transient) is True
    assert is_rate_limited(permanent) is False


async def test_run_cases_stops_starting_cases_once_the_breaker_trips(tmp_path: Path) -> None:
    cases = [
        EvalCase.from_dict({"case_id": f"c-{n}", "kind": "benign", "email": {"body_text": "hello"}})
        for n in range(6)
    ]
    started: list[str] = []

    async def execute(case: EvalCase) -> dict[str, Any]:
        started.append(case.case_id)
        raise RuntimeError("provider_mismatch: served by 'Groq'")

    store = ResultStore(tmp_path / "rows.jsonl")
    breaker = RouteBreaker()

    async def no_sleep(_: float) -> None:
        return None

    summary = await run_cases(
        cases,
        execute,
        store,
        config_name="C0",
        run_id="r",
        policy=BackoffPolicy(max_attempts=1),
        sleep=no_sleep,
        breaker=breaker,
    )

    assert started == ["c-0", "c-1", "c-2"]  # the other three never started: a resume runs them
    assert summary.error == 3
    assert summary.stopped is not None and "provider_mismatch" in summary.stopped
    assert sorted(store.latest_records()) == ["c-0", "c-1", "c-2"]


async def test_run_cases_without_a_breaker_runs_every_case(tmp_path: Path) -> None:
    cases = [
        EvalCase.from_dict({"case_id": f"c-{n}", "kind": "benign", "email": {"body_text": "x"}})
        for n in range(4)
    ]

    async def execute(case: EvalCase) -> dict[str, Any]:
        raise RuntimeError("provider_mismatch: served by 'Groq'")

    summary = await run_cases(
        cases,
        execute,
        ResultStore(tmp_path / "rows.jsonl"),
        config_name="C0",
        run_id="r",
        policy=BackoffPolicy(max_attempts=1),
    )

    assert summary.error == 4 and summary.stopped is None


# --- the run meta and the summary ---------------------------------------------------------


def routed_llm() -> LLMTiersSettings:
    env = profile_env(get_profile("llama-3.1-8b-openrouter"), {"BENCH_OPENROUTER_API_KEY": "k"})
    import os
    from unittest.mock import patch

    with patch.dict(os.environ, env):
        return AppSettings(_env_file=None).llm


def test_the_meta_of_a_routed_run_records_the_pin_and_the_metadata_flag() -> None:
    meta = generation_meta(routed_llm())

    assert meta["generation"]["provider_routing"] == ROUTING_DICT
    assert meta["generation"]["response_metadata"] is True
    assert meta["generation_model"] == LLAMA
    assert route_meta(routed_llm()) == {
        "provider_routing": ROUTING_DICT,
        "response_metadata": True,
    }


def test_the_meta_of_an_unrouted_run_is_unchanged() -> None:
    meta = generation_meta(LLMTiersSettings(provider="openai", openai_api_key="k"))

    assert "provider_routing" not in meta["generation"]
    assert "response_metadata" not in meta["generation"]
    assert route_meta(LLMTiersSettings()) is None


def test_a_resume_under_another_pin_is_a_settings_change() -> None:
    from evaluation.mailguard_bench.runner import settings_fingerprint

    a = settings_fingerprint(generation_meta(routed_llm()))
    other = routed_llm().model_copy(
        update={"openai_provider_routing": ProviderRouting(order=["deepinfra"])}
    )
    b = settings_fingerprint(generation_meta(other))

    assert a != b


def _row(generation: list[dict[str, Any]] | None, guard: list[dict[str, Any]] | None) -> Any:
    result: dict[str, Any] = {"generation": {"called": True}, "guard_llm": {"calls": 0}}
    if generation is not None:
        result["generation"]["provenance"] = generation
    if guard is not None:
        result["guard_llm"]["provenance"] = guard
    return {"case_id": "x", "status": "ok", "result": result}


def test_the_summary_counts_calls_per_served_provider_and_role() -> None:
    rows = [
        _row([prov().to_dict()], [prov().to_dict(), prov().to_dict()]),
        _row([prov("DeepInfra", 2, cost=0.00001).to_dict()], None),
        _row([prov(None, None).to_dict()], []),
        {"case_id": "e", "status": "error", "result": None},
        {"case_id": "v1", "status": "ok", "result": {"generation": {"called": True}}},
    ]

    summary = provenance_summary(rows)

    assert summary is not None
    assert summary["calls"] == 5
    assert summary["by_role"] == {"generation": 3, "guard": 2}
    assert summary["by_provider"] == {"CoreWeave": 3, "DeepInfra": 1, "unknown": 1}
    assert summary["fallback_attempts"] == 1  # attempt above 1
    assert summary["unverified"] == 1  # no served provider in the response
    assert summary["cost_usd"] == pytest.approx(0.000005 * 4 + 0.00001)
    assert summary["prompt_tokens"] == 50 and summary["completion_tokens"] == 20


def test_rows_without_provenance_summarize_to_nothing() -> None:
    assert provenance_summary([_row(None, None), {"case_id": "e", "result": None}]) is None


def test_pin_problem_checks_a_guard_call_against_the_providers_own_pin() -> None:
    assert pin_problem(None, prov("Groq").to_dict()) is None  # not pinned: nothing to check
    assert pin_problem({}, None) is None
    assert pin_problem(ROUTING_DICT, prov().to_dict()) is None
    assert "Groq" in (pin_problem(ROUTING_DICT, prov("Groq").to_dict()) or "")
    assert pin_problem(ROUTING_DICT, None) is not None  # a pin the response cannot show is unmet


def test_a_guard_provider_that_sends_the_runs_pin_passes() -> None:
    provider = GuardStub(prov(), ROUTING_DICT)
    provider.response_metadata = True  # type: ignore[attr-defined]
    assert guard_pin_problem(provider, ROUTING_DICT) is None
    assert guard_pin_problem(provider, None) is None  # not a routed run: nothing is expected


def test_a_guard_without_routing_support_cannot_run_a_pinned_run() -> None:
    # The guard at 1a3ef62 ignores `provider_routing` in guard_models.yaml: its judges would run
    # on whatever OpenRouter picks and report no provider, with nothing to show it.
    old_guard_provider = GuardStub(None, routing=None)
    problem = guard_pin_problem(old_guard_provider, ROUTING_DICT)
    assert problem is not None and "provider routing" in problem and "coreweave" in problem


def test_a_guard_pin_that_differs_from_the_profiles_is_refused() -> None:
    provider = GuardStub(None, {**ROUTING_DICT, "order": ["deepinfra"]})
    provider.response_metadata = True  # type: ignore[attr-defined]
    assert guard_pin_problem(provider, ROUTING_DICT) is not None


def test_a_pin_without_the_metadata_switch_cannot_be_verified() -> None:
    provider = GuardStub(None, ROUTING_DICT)  # response_metadata missing
    problem = guard_pin_problem(provider, ROUTING_DICT)
    assert problem is not None and "metadata" in problem


def test_the_expected_guard_route_is_the_settings_pin() -> None:
    assert expected_guard_route(routed_llm()) == ROUTING_DICT
    assert expected_guard_route(LLMTiersSettings()) is None


# --- the documentation and the example env -----------------------------------------------

REPO = Path(__file__).resolve().parents[2]
ROUTE_KEYS = (
    "LLM__OPENAI_PROVIDER_ROUTING",
    "LLM__OPENAI_RESPONSE_METADATA",
    "BENCH_OPENROUTER_API_KEY",
    "BENCH_OPENROUTER_BASE_URL",
)


def test_every_route_key_is_in_env_example_but_none_is_set_in_a_fresh_copy() -> None:
    from dotenv import dotenv_values

    text = (REPO / ".env.example").read_text(encoding="utf-8")
    active = dotenv_values(REPO / ".env.example")
    for key in ROUTE_KEYS:
        assert f"# {key}=" in text, key
        # Set in a fresh copy they would route every run through OpenRouter (or forward a blank).
        assert key not in active, key


def test_every_route_key_is_in_the_configuration_reference() -> None:
    text = (REPO / "docs/configuration.md").read_text(encoding="utf-8")
    for key in ROUTE_KEYS:
        assert f"`{key}`" in text, key
    assert "| `LLM__OPENAI_PROVIDER_ROUTING` | `JSON object` |" in text
    assert "| `LLM__OPENAI_RESPONSE_METADATA` | `boolean` |" in text


def test_the_runbook_section_names_the_profiles_the_canary_and_the_stop_rule() -> None:
    from evaluation.mailguard_bench.openrouter_canary import parse_args

    text = (REPO / "docs/demo-runbook.md").read_text(encoding="utf-8")
    section = text[text.index("### 9.10 ") : text.index("## Appendix A")]

    for profile in ("qwen2.5-7b-openrouter", "llama-3.1-8b-openrouter"):
        assert profile in section
    for phrase in ("BENCH_OPENROUTER_API_KEY", "STOP", "provider_mismatch", "not comparable"):
        assert phrase in section, phrase
    # each pinned model has its canary command (the kit's Make target), and its profile parses
    commands = [
        line.split("MODEL=", 1)[1].split()[0]
        for line in section.splitlines()
        if line.startswith("make bench-canary MODEL=")
    ]
    assert len(commands) == 2
    assert {parse_args(["--model-profile", name]).model_profile for name in commands} == {
        "qwen2.5-7b-openrouter",
        "llama-3.1-8b-openrouter",
    }
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "openrouter_canary --model-profile $(MODEL)" in makefile
    # step 1's key block is a different section, and the tests of §9.9 read it
    assert "BENCH_OPENROUTER_API_KEY=<your OpenRouter key>" in text


def test_the_stop_message_says_a_resume_needs_retry_errors() -> None:
    """The rows that tripped the stop are error rows; a plain resume skips recorded cases."""
    message = stop_message("C0", "3 consecutive provider_mismatch errors")

    assert message.startswith("STOP C0: 3 consecutive provider_mismatch errors")
    assert "--retry-errors" in message


def test_the_stop_message_tells_a_kit_user_to_rerun_the_kit_command_unchanged() -> None:
    """`make bench-run` has no retry option and always passes --retry-errors to the runner (the
    kit's command tests check that), so a teammate who reads the STOP line under the kit must
    not look for a flag to add."""
    message = stop_message("C0", "no_credit")

    assert "`make bench-run` command unchanged" in message
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    recipe = makefile.split("\nbench-run:", 1)[1].split("\nbench-report:", 1)[0]
    assert "retry" not in recipe.lower()


# --- the report states the route and what the runs are not comparable with ----------------


ROUTE_FACTS = {
    "pin": {"order": ["phala"], "allow_fallbacks": False, "require_parameters": True},
    "provenance": {
        "calls": 12,
        "by_provider": {"Phala": 12},
        "fallback_attempts": 0,
        "unverified": 0,
        "cost_usd": 0.0042,
    },
}


def test_a_routed_runs_limitations_state_the_pin_the_providers_and_the_comparability() -> None:
    from evaluation.mailguard_bench.threat_model import render_threat_model

    text = " ".join(render_threat_model("qwen/qwen-2.5-7b-instruct", route=ROUTE_FACTS).split())

    assert "OpenRouter" in text
    assert "phala" in text and "Phala: 12" in text
    assert "0 fallback attempts" in text and "0 unverified" in text
    assert "not comparable with the local 4-bit runs" in text


def test_an_unrouted_runs_limitations_do_not_mention_openrouter() -> None:
    from evaluation.mailguard_bench.threat_model import render_threat_model

    assert "OpenRouter" not in render_threat_model("qwen2.5:7b-instruct")


def test_the_run_facts_read_the_pin_and_the_served_providers_from_the_meta(
    tmp_path: Path,
) -> None:
    from evaluation.mailguard_bench.analyses import run_facts

    (tmp_path / "raw").mkdir()
    meta = {
        "generation_model": "qwen/qwen-2.5-7b-instruct",
        "generation": {"provider_routing": ROUTE_FACTS["pin"], "response_metadata": True},
        "invocations": [
            {"summary": {"provenance": {"calls": 4, "by_provider": {"Phala": 4}}}},
            {"summary": {"provenance": ROUTE_FACTS["provenance"]}},
        ],
    }
    (tmp_path / "raw" / "C3.meta.json").write_text(json.dumps(meta), encoding="utf-8")

    assert run_facts(tmp_path).route == ROUTE_FACTS  # the latest invocation totals every row

    meta.pop("generation")
    (tmp_path / "raw" / "C3.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert run_facts(tmp_path).route is None


async def test_a_plain_resume_skips_the_rows_that_tripped_the_stop_and_retry_errors_reruns_them(
    tmp_path: Path,
) -> None:
    cases = [
        EvalCase.from_dict({"case_id": f"c-{n}", "kind": "benign", "email": {"body_text": "x"}})
        for n in range(5)
    ]
    store = ResultStore(tmp_path / "rows.jsonl")
    calls: list[str] = []
    serving = {"ok": False}

    async def execute(case: EvalCase) -> dict[str, Any]:
        calls.append(case.case_id)
        if not serving["ok"]:
            raise RuntimeError("provider_mismatch: served by 'Groq'")
        return {"final_action": "reply"}

    async def no_sleep(_: float) -> None:
        return None

    def go(**kw: Any) -> Any:
        return run_cases(
            cases,
            execute,
            store,
            config_name="C0",
            run_id="r",
            policy=BackoffPolicy(max_attempts=1),
            sleep=no_sleep,
            breaker=RouteBreaker(),
            **kw,
        )

    first = await go()
    assert first.stopped is not None and calls == ["c-0", "c-1", "c-2"]

    serving["ok"] = True
    calls.clear()
    plain = await go()
    assert calls == ["c-3", "c-4"]  # the three error rows are skipped, as stop_message warns
    assert plain.skipped == 3

    calls.clear()
    retried = await go(retry_errors=True)
    assert sorted(calls) == ["c-0", "c-1", "c-2"]
    assert retried.ok == 3


def test_a_finished_invocation_records_the_served_providers_and_why_it_stopped(
    tmp_path: Path,
) -> None:
    from evaluation.mailguard_bench.runner import RunSummary, add_route_summary

    store = ResultStore(tmp_path / "rows.jsonl")
    record = {
        "case_id": "c-0",
        "config": "C0",
        "status": "ok",
        "result": {"generation": {"provenance": [prov().to_dict()]}},
    }
    store.append(record)
    invocation: dict[str, Any] = {}

    add_route_summary(invocation, store, RunSummary(selected=1, stopped="3 consecutive x"))

    assert invocation["stopped"] == "3 consecutive x"
    assert invocation["provenance"]["by_provider"] == {"CoreWeave": 1}
    bare: dict[str, Any] = {}
    add_route_summary(bare, ResultStore(tmp_path / "none.jsonl"), RunSummary(selected=0))
    assert bare == {}  # an unrouted run's summary keeps its old shape
