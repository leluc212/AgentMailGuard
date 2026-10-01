"""Guard route failures become error rows; quota and credit stops for every provider (task 7.29).

Owner decisions A and B of 2026-10-01 (ADR-0014). A guard LLM call that failed on the route or
the service (HTTP 402, 404, 408, 409, 429, 5xx, a timeout, a connection error, a router error in
an HTTP 200 body) makes its case an error row the retry pass reruns; a model answering badly stays
the guard's scored fallback. Every run stops at once on a used-up quota or credit; only a pinned
run stops on a streak of lost-route rows. Stand-in providers with the guard's exception chaining;
no network.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from evaluation.mailguard_bench.case_adapter import EvalCase
from evaluation.mailguard_bench.counting import CountingProvider
from evaluation.mailguard_bench.resilience import BackoffPolicy, exhausted_quota, is_rate_limited
from evaluation.mailguard_bench.results import ResultStore
from evaluation.mailguard_bench.route import (
    GUARD_ROUTE_FAILURE,
    RouteBreaker,
    guard_call_failure,
    guard_result_failure,
    route_failure,
    stop_message,
)
from evaluation.mailguard_bench.runner import progress_line, run_cases
from evaluation.mailguard_bench.scoring import GUARD_ROUTE_FAILURE_KIND
from packages.llm.protocol import LLMQuotaExhaustedError, LLMResponseError, LLMResult, ModelTier

URL = "https://openrouter.ai/api/v1/chat/completions"


class GuardLLMError(Exception):
    """What AgentMailGuard's providers raise: a message, the transport's error chained."""


def http_failure(status: int, body: Any = None) -> GuardLLMError:
    request = httpx.Request("POST", URL)
    response = httpx.Response(status, json=body or {"error": {"message": "x"}}, request=request)
    cause = httpx.HTTPStatusError(str(status), request=request, response=response)
    try:
        raise GuardLLMError(f"OpenAI HTTP {status}: {response.text[:300]}") from cause
    except GuardLLMError as exc:
        return exc


def chained(message: str, cause: BaseException) -> GuardLLMError:
    try:
        raise GuardLLMError(message) from cause
    except GuardLLMError as exc:
        return exc


@pytest.mark.parametrize("status", [402, 404, 408, 409, 429, 500, 502, 503, 504])
def test_the_owner_s_statuses_are_route_failures(status: int) -> None:
    note = guard_call_failure(http_failure(status))
    assert note is not None and note.startswith(f"HTTP {status}")


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_other_statuses_are_not_route_failures(status: int) -> None:
    assert guard_call_failure(http_failure(status)) is None


def test_a_timeout_and_a_connection_error_are_route_failures() -> None:
    request = httpx.Request("POST", URL)
    timeout = chained("timed out", httpx.ReadTimeout("slow", request=request))
    refused = chained("transport", httpx.ConnectError("refused", request=request))

    assert guard_call_failure(timeout) == "timed out"
    assert guard_call_failure(refused) == "a connection error"


def test_a_402_note_keeps_openrouters_limit_source_and_a_429_its_used_up_quota() -> None:
    no_credit = http_failure(402, {"error": {"metadata": {"limit_source": "openrouter_credits"}}})
    quota = http_failure(429, {"error": {"code": "credit_balance_exhausted", "message": "x"}})

    assert guard_call_failure(no_credit) == "HTTP 402 (openrouter_credits)"
    assert guard_call_failure(quota) == "HTTP 429 (quota_exhausted: credit_balance_exhausted)"


def test_a_router_error_in_an_http_200_body_is_a_route_failure_and_an_odd_answer_is_not() -> None:
    body = {"error": {"code": 502, "message": "upstream down"}}
    with_error = GuardLLMError(f"Empty choices from OpenAI: {body!r}")
    without = GuardLLMError("Empty choices from OpenAI: {'id': 'gen-1', 'choices': []}")

    assert guard_call_failure(with_error) == "a router error in an HTTP 200 body (HTTP 502)"
    assert guard_call_failure(without) is None


@pytest.mark.parametrize(
    ("message", "note"),
    [
        ("OpenAI HTTP 503: no provider", "HTTP 503"),
        ("Ollama HTTP 500: crashed", "HTTP 500"),
        ("OpenAI HTTP 401: bad key", None),
        ("OpenAI request timed out after 60.0s", "timed out"),
        ("OpenAI transport error: [Errno 111] refused", "a connection error"),
    ],
)
def test_the_guard_providers_own_messages_are_read_when_nothing_is_chained(
    message: str, note: str | None
) -> None:
    assert guard_call_failure(GuardLLMError(message)) == note


def test_a_model_answering_badly_is_never_a_route_failure() -> None:
    # The guard raises its schema error after generate() returned: no transport error is chained.
    assert guard_call_failure(GuardLLMError("non_json: the model answered in prose")) is None


def answer(raw: dict[str, Any], finish: str = "stop") -> LLMResult:
    return LLMResult(
        content={"raw_text": ""},
        model="m",
        tier=ModelTier.FAST,
        raw_finish_reason=finish,
        raw_response=raw,
    )


def test_a_200_answer_that_carries_an_error_is_a_route_failure() -> None:
    assert guard_result_failure(answer({"error": {"code": 503, "message": "x"}})) == (
        "a router error in an HTTP 200 body (HTTP 503)"
    )
    in_choice = {"choices": [{"error": {"code": 502}, "finish_reason": "error"}]}
    assert guard_result_failure(answer(in_choice)) is not None
    assert guard_result_failure(answer({"choices": []}, finish="error")) is not None
    assert guard_result_failure(answer({"choices": [{"message": {"content": "{}"}}]})) is None


# --- CountingProvider records them per case ---------------------------------------------------


class FailingGuardProvider:
    """A guard provider: answers, or raises like AgentMailGuard's OpenAIProvider."""

    def __init__(self, outcomes: Sequence[BaseException | LLMResult]) -> None:
        self.model = "m"
        self.provider_routing = None
        self._outcomes = list(outcomes)

    async def generate(self, **kwargs: Any) -> Any:
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def test_counting_records_a_route_failure_and_still_lets_the_stage_fall_back() -> None:
    counting = CountingProvider(
        FailingGuardProvider([http_failure(503), answer({"choices": [{"message": {}}]})])
    )
    counting.begin_case()

    with pytest.raises(GuardLLMError):  # the guard's stage catches it and falls back
        await counting.generate(messages=[])
    await counting.generate(messages=[])  # a bad answer: the guard's own fallback
    snapshot = counting.snapshot()

    (failure,) = snapshot["route_failures"]
    assert failure.startswith(f"{GUARD_ROUTE_FAILURE}: ") and "HTTP 503" in failure
    assert len(snapshot["errors"]) == 1


async def test_counting_records_a_router_error_in_a_200_body() -> None:
    counting = CountingProvider(FailingGuardProvider([answer({"error": {"code": 502}})]))
    counting.begin_case()

    await counting.generate(messages=[])

    (failure,) = counting.snapshot()["route_failures"]
    assert "a router error in an HTTP 200 body (HTTP 502)" in failure


async def test_a_clean_case_keeps_the_snapshot_shape() -> None:
    counting = CountingProvider(FailingGuardProvider([answer({"choices": [{}]})]))
    counting.begin_case()

    await counting.generate(messages=[])

    assert "route_failures" not in counting.snapshot()


# --- the row and the breaker ------------------------------------------------------------------


def case(n: int) -> EvalCase:
    return EvalCase.from_dict({"case_id": f"c-{n}", "kind": "attack", "email": {"body_text": "x"}})


async def no_sleep(_: float) -> None:
    return None


async def test_a_guarded_result_with_a_route_failure_is_an_error_row_of_its_own_kind(
    tmp_path: Path,
) -> None:
    async def execute(c: EvalCase) -> dict[str, Any]:
        return {
            "final_action": "reply",
            "guard_errors": [],
            "guard_route_failures": [f"{GUARD_ROUTE_FAILURE}: ...: HTTP 503: down"],
        }

    store = ResultStore(tmp_path / "rows.jsonl")
    summary = await run_cases([case(0)], execute, store, config_name="C7", run_id="r")

    row = store.latest_records()["c-0"]
    assert summary.error == 1 and row["status"] == "error"
    assert row["error"]["kind"] == GUARD_ROUTE_FAILURE_KIND == GUARD_ROUTE_FAILURE
    assert "HTTP 503" in row["error"]["message"]
    assert row["result"]["guard_route_failures"]  # kept for the reader of the raw row


async def test_a_retry_pass_reruns_a_guard_route_failure_row(tmp_path: Path) -> None:
    failing = {"on": True}

    async def execute(c: EvalCase) -> dict[str, Any]:
        failures = [f"{GUARD_ROUTE_FAILURE}: x: timed out"] if failing["on"] else []
        return {"final_action": "reply", "guard_route_failures": failures}

    store = ResultStore(tmp_path / "rows.jsonl")
    await run_cases([case(0)], execute, store, config_name="C7", run_id="r")
    failing["on"] = False
    again = await run_cases(
        [case(0)], execute, store, config_name="C7", run_id="r", retry_errors=True
    )

    assert again.ok == 1 and store.latest_records()["c-0"]["status"] == "ok"


def error_row(kind: str, message: str) -> dict[str, Any]:
    return {"status": "error", "error": {"kind": kind, "message": message}}


QUOTA_ROW = error_row(
    "PipelineJobError",
    "case c: job j ended DEAD_LETTER: FatalError: LLMQuotaExhaustedError: LLM request failed "
    "with status 429 (quota_exhausted: insufficient_quota): {...}",
)
GUARD_503 = error_row(GUARD_ROUTE_FAILURE, f"{GUARD_ROUTE_FAILURE}: a guard LLM call: HTTP 503")
MISMATCH = error_row("guard_layer_error", "provider_mismatch: served by 'DeepInfra'")


def test_a_used_up_quota_is_a_route_failure_of_its_own() -> None:
    assert route_failure(QUOTA_ROW) == "quota_exhausted"
    assert route_failure(GUARD_503) == "no_provider"


@pytest.mark.parametrize("routed", [True, False])
def test_every_run_stops_at_once_on_a_used_up_quota(routed: bool) -> None:
    breaker = RouteBreaker(routed=routed)
    breaker.observe(QUOTA_ROW)

    assert breaker.tripped is not None
    assert breaker.tripped.startswith("quota_exhausted: insufficient_quota")


def test_an_unpinned_run_also_stops_on_no_credit_but_never_on_a_streak() -> None:
    breaker = RouteBreaker(routed=False)
    for record in (GUARD_503, GUARD_503, MISMATCH, GUARD_503, GUARD_503):
        breaker.observe(record)
    assert breaker.tripped is None  # OpenAI's 5xx rows are retried, there is no pin to lose

    no_credit = error_row("PipelineJobError", "LLM request failed with status 402: no credit")
    breaker.observe(no_credit)
    assert breaker.tripped is not None and breaker.tripped.startswith("no_credit")


def test_a_pinned_run_stops_on_three_guard_503s_in_a_row() -> None:
    breaker = RouteBreaker(routed=True)
    for _ in range(3):
        breaker.observe(GUARD_503)
    assert breaker.tripped is not None and "no_provider" in breaker.tripped


async def test_an_unrouted_run_stops_starting_cases_after_a_quota_row(tmp_path: Path) -> None:
    started: list[str] = []

    async def execute(c: EvalCase) -> dict[str, Any]:
        started.append(c.case_id)
        raise RuntimeError(str(QUOTA_ROW["error"]["message"]))

    summary = await run_cases(
        [case(n) for n in range(4)],
        execute,
        ResultStore(tmp_path / "rows.jsonl"),
        config_name="C0",
        run_id="r",
        policy=BackoffPolicy(max_attempts=6),
        sleep=no_sleep,
        breaker=RouteBreaker(routed=False),
    )

    assert started == ["c-0"]  # not retried with back-off, and no further case started
    assert summary.stopped is not None and summary.stopped.startswith("quota_exhausted")


def test_the_stop_line_says_what_to_restore_and_that_other_models_may_run_in_between() -> None:
    quota = stop_message("C0", "quota_exhausted: daily_limit; a provider's quota is used up")
    route = stop_message("C0", "3 consecutive no_provider errors; the route is not serving")

    assert "daily cap resets the next day" in quota and "restore it" in quota
    assert "once the route serves again" in route
    for line in (quota, route):
        assert "`make bench-run` command unchanged" in line
        assert "other models' runs may run in between" in line
        assert "--retry-errors" in line


JOB = "job 0b5d1f3e-8f4a-4c55-9a43-2f1d6f0f9c11"


def _stop_line(row: dict[str, Any]) -> str:
    breaker = RouteBreaker(routed=False)
    breaker.observe(row)
    assert breaker.tripped is not None
    return stop_message("C0", breaker.tripped)


def test_the_stop_line_names_the_service_whose_daily_cap_ran_out() -> None:
    # The same daily cap of two services: the embedding (a query embedding the ai-worker
    # recorded) and the model (a job the ai-worker dead-lettered). The runner must see which
    # account to restore without opening the raw file.
    embedding = {
        "case_id": "attack-prag-a",
        **error_row(
            "retrieval_degraded",
            f"case attack-prag-a: {JOB}: retrieval degraded (the vector branch failed: Embedding "
            "request failed with status 429 (quota_exhausted: daily_limit))",
        ),
    }
    model = {
        "case_id": "attack-llmail-b",
        **error_row(
            "PipelineJobError",
            f"case attack-llmail-b: {JOB} ended DEAD_LETTER: FatalError: LLMQuotaExhaustedError: "
            "LLM request failed with status 429 (quota_exhausted: daily_limit): {"
            + "x" * 400
            + "}",
        ),
    }

    by_embedding, by_model = _stop_line(embedding), _stop_line(model)

    assert by_embedding != by_model
    for line in (by_embedding, by_model):
        assert line.startswith("STOP C0: quota_exhausted: daily_limit; ")
        assert "daily cap resets the next day" in line  # the advice still reads the reason
    assert "case attack-prag-a, retrieval_degraded: " in by_embedding
    assert "Embedding request failed with status 429" in by_embedding
    assert "case attack-llmail-b, PipelineJobError: " in by_model
    assert "LLMQuotaExhaustedError: LLM request failed with status 429" in by_model
    assert "x" * 100 not in by_model  # an excerpt, never the whole body


def test_a_no_credit_stop_and_a_streak_stop_name_their_source_too() -> None:
    no_credit = {
        "case_id": "c-9",
        **error_row(GUARD_ROUTE_FAILURE, f"{GUARD_ROUTE_FAILURE}: a guard LLM call: HTTP 402"),
    }
    assert "case c-9, guard_route_failure: " in _stop_line(no_credit)
    assert "HTTP 402" in _stop_line(no_credit)

    breaker = RouteBreaker(routed=True)
    for n in range(3):
        breaker.observe({"case_id": f"c-{n}", **GUARD_503})
    assert breaker.tripped is not None
    assert "(the last: case c-2, guard_route_failure: " in breaker.tripped


def test_an_error_progress_line_names_its_kind() -> None:
    row = {"case_id": "c-1", "attempts": 1, **error_row("retrieval_degraded", "long message")}
    assert progress_line(row) == "error c-1 (attempts=1) retrieval_degraded"
    ok = {"case_id": "c-2", "attempts": 2, "status": "ok", "error": None}
    assert progress_line(ok) == "ok    c-2 (attempts=2)"


# --- the benchmark's view of a used-up quota (owner decision B) --------------------------------


def dead_lettered(exc: BaseException) -> dict[str, Any]:
    """The error row of a job the ai-worker dead-lettered for ``exc``, cut to 200 characters as
    a triage stage's error is."""
    text = f"FatalError: {type(exc).__name__}: {exc}"[:200]
    return error_row("PipelineJobError", text)


def test_the_clients_quota_error_stops_the_breaker_and_is_never_backed_off() -> None:
    body = "{" + " " * 400 + '"code": "insufficient_quota"}'  # a long body after the head
    used_up = LLMQuotaExhaustedError(
        f"LLM request failed with status 429 (quota_exhausted: insufficient_quota): {body}",
        quota="insufficient_quota",
    )
    per_minute = LLMResponseError("LLM request failed with status 429: slow down", status_code=429)

    assert route_failure(dead_lettered(used_up)) == "quota_exhausted"
    assert not is_rate_limited(used_up)
    assert route_failure(dead_lettered(per_minute)) is None
    assert is_rate_limited(per_minute)


def test_a_guard_providers_chained_http_error_is_read_for_the_quota() -> None:
    # AgentMailGuard's provider cuts the body at 300 characters in its message but chains the
    # httpx error, whose response holds the whole body.
    quota = http_failure(429, {"error": {"code": "credit_balance_exhausted", "message": "x"}})
    per_minute = http_failure(429, {"error": {"code": "slow_down", "message": "slow down"}})

    assert exhausted_quota(quota) == "credit_balance_exhausted"
    assert not is_rate_limited(quota)
    assert exhausted_quota(per_minute) is None
    assert is_rate_limited(per_minute)
