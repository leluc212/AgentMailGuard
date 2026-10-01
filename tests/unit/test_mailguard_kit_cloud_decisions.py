"""The kit under the owner decisions of 2026-10-01 (task 7.29; ADR-0014): the one-call embedding
check before any model spend (D), the trial over chosen cases (E), and a run stopped by a limit that
resumes after another model's run (F).

The kit's fakes (``mailguard_kit_fixtures``) stand in for docker, the runner and the guard-worker;
the embedding endpoint is an ``httpx.MockTransport``. No test reaches a real endpoint or a model.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from evaluation.mailguard_bench.kit import campaign
from evaluation.mailguard_bench.kit.campaign import (
    RunOptions,
    resume_commands,
    run_campaign,
)
from evaluation.mailguard_bench.kit.embedding_check import (
    PROBE_TEXT,
    EmbeddingCheckError,
    check_embedding,
    embedding_settings,
    probe_embedding,
)
from evaluation.mailguard_bench.kit.steplog import finished_configs
from evaluation.mailguard_bench.route import ROUTE_STOP_EXIT
from tests.unit.mailguard_kit_fixtures import (  # noqa: F401  (bench_fixture is the `bench` fixture)
    EMBED_KEY,
    HOST_ENV,
    OPENAI_KEY,
    STACK_UP,
    Bench,
    EmbeddingEndpoint,
    bench_fixture,
    label,
    opts,
    sequence,
)

REPO = Path(__file__).resolve().parents[2]


# --- D: one embedding call before any model spend ---------------------------------------------


def test_the_run_makes_one_embedding_call_through_the_services_embedder_before_the_stack(
    bench: Bench,
) -> None:
    assert run_campaign(bench.ctx, opts(configs=("C0",))) == 0

    (request,) = bench.embedding.requests
    sent = json.loads(request.content)
    assert request.url == "https://generativelanguage.googleapis.com/v1beta/openai/embeddings"
    assert sent == {"input": [PROBE_TEXT], "model": "gemini-embedding-001", "dimensions": 1536}
    assert request.headers["authorization"] == f"Bearer {EMBED_KEY}"
    steps = [s["step"] for s in bench.kit_log()]
    assert steps.index("embedding_check") < steps.index("stack")
    (check,) = [s for s in bench.kit_log() if s["step"] == "embedding_check"]
    assert (check["status"], check["dimension"], check["embedding_model"]) == (
        "ok",
        1536,
        "gemini-embedding-001",
    )
    printed = "\n".join(bench.out + bench.err)
    assert "ok embedding gemini-embedding-001" in printed
    assert EMBED_KEY not in printed and EMBED_KEY not in json.dumps(bench.kit_log())


def test_an_endpoint_that_ignores_dimensions_stops_the_run_before_the_stack_is_touched(
    bench: Bench,
) -> None:
    bench.embedding.width = 3072  # gemini-embedding-001's own width, when `dimensions` is ignored

    assert run_campaign(bench.ctx, opts()) == 1

    assert STACK_UP not in sequence(bench.host) and "live.run C0" not in sequence(bench.host)
    text = "\n".join(bench.err)
    assert "embedding check refused this run" in text
    assert "did not return 1536-dimension vectors" in text and "ignores the `dimensions`" in text
    (check,) = [s for s in bench.kit_log() if s["step"] == "embedding_check"]
    assert check["status"] == "failed" and "3072" in check["reason"]


@pytest.mark.parametrize(
    ("status", "body", "words"),
    [
        (400, {"error": {"message": "dimensions not supported"}}, "text-embedding-ada-002"),
        (401, {"error": {"message": "bad key"}}, "refused the key"),
        (404, {"error": {"message": "no model"}}, "no such model or URL"),
        (
            429,
            {"error": {"message": "x", "type": "insufficient_quota", "code": "insufficient_quota"}},
            "quota_exhausted: insufficient_quota",
        ),
    ],
)
def test_a_refused_embedding_call_is_one_request_and_says_what_to_fix(
    bench: Bench, status: int, body: dict[str, Any], words: str
) -> None:
    bench.embedding.status, bench.embedding.body = status, body

    assert run_campaign(bench.ctx, opts()) == 1

    assert len(bench.embedding.requests) == 1  # one call, never the embedder's retries
    assert words in "\n".join(bench.err)
    assert STACK_UP not in sequence(bench.host)


PER_MINUTE = {"error": {"message": "Rate limit reached for requests per min (RPM)"}}


def test_a_per_minute_rate_limit_is_waited_out_once_and_the_run_goes_on(bench: Bench) -> None:
    # Owner decision B: a per-minute 429 is backed off, never a stop. The check waits the
    # answer's Retry-After and sends the same request once more.
    bench.embedding.queued = [httpx.Response(429, json=PER_MINUTE, headers={"Retry-After": "7"})]
    began = bench.host.now

    assert run_campaign(bench.ctx, opts(configs=("C0",))) == 0

    first, second = bench.embedding.requests
    assert first.content == second.content  # the same one-line request, nothing else embedded
    assert bench.host.now - began >= 7
    assert any("per-minute rate limit (HTTP 429): waiting 7 s" in line for line in bench.out)
    (check,) = [s for s in bench.kit_log() if s["step"] == "embedding_check"]
    assert check["status"] == "ok"


@pytest.mark.parametrize(("retry_after", "waited"), [(None, 60.0), ("3600", 60.0), ("2", 2.0)])
async def test_the_wait_is_the_retry_after_capped_at_a_minute(
    retry_after: str | None, waited: float
) -> None:
    endpoint = EmbeddingEndpoint()
    headers = {"Retry-After": retry_after} if retry_after else {}
    endpoint.queued = [httpx.Response(429, json=PER_MINUTE, headers=headers)]
    waits: list[float] = []

    async def wait(seconds: float) -> None:
        waits.append(seconds)

    found = await probe_embedding(
        embedding_settings(HOST_ENV), transport=endpoint.transport(), sleep=wait
    )

    assert waits == [waited] and found.dimension == 1536


def test_a_second_rate_limit_refuses_after_two_requests(bench: Bench) -> None:
    bench.embedding.status, bench.embedding.body = 429, PER_MINUTE

    assert run_campaign(bench.ctx, opts()) == 1

    assert len(bench.embedding.requests) == 2  # one more try, never the embedder's ladder
    assert "wait a minute, then run the same command again" in "\n".join(bench.err).lower()
    assert STACK_UP not in sequence(bench.host)


def test_a_used_up_quota_is_never_waited_out() -> None:
    endpoint = EmbeddingEndpoint()
    endpoint.status, endpoint.body = 429, {"error": {"code": "insufficient_quota", "message": "x"}}
    waits: list[float] = []

    with pytest.raises(EmbeddingCheckError, match="quota_exhausted: insufficient_quota"):
        check_embedding(HOST_ENV, transport=endpoint.transport(), sleep=waits.append)

    assert waits == [] and len(endpoint.requests) == 1


def test_a_dry_run_names_the_embedding_check_and_calls_nothing(bench: Bench) -> None:
    assert run_campaign(bench.ctx, opts(dry_run=True)) == 0

    assert bench.embedding.requests == []
    assert any("one embedding call" in line for line in bench.out)


async def test_the_fake_embedder_is_never_checked_or_accepted() -> None:
    settings = embedding_settings({**HOST_ENV, "EMBEDDING__MOCK": "true"})
    with pytest.raises(EmbeddingCheckError, match="fake embedder"):
        await probe_embedding(settings, transport=EmbeddingEndpoint().transport())


async def test_an_empty_answer_is_refused_rather_than_padded_with_zeros() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "usage": {}})

    with pytest.raises(EmbeddingCheckError, match="0 vector"):
        await probe_embedding(embedding_settings(HOST_ENV), transport=httpx.MockTransport(handler))


def test_the_check_reads_the_runners_settings_for_one_request() -> None:
    settings = embedding_settings({**HOST_ENV, "EMBEDDING__MAX_RETRIES": "5", "OTHER": "x"})
    assert settings.max_retries == 0 and settings.mock is False
    assert settings.model_name == "gemini-embedding-001" and settings.dimension == 1536
    with pytest.raises(EmbeddingCheckError, match="EMBEDDING__DIMENSION") as caught:
        embedding_settings({**HOST_ENV, "EMBEDDING__DIMENSION": "wide"})
    assert EMBED_KEY not in str(caught.value)


def test_the_doctor_stays_free_of_calls() -> None:
    doctor = (REPO / "evaluation" / "mailguard_bench" / "kit" / "doctor.py").read_text("utf-8")
    assert "embedding_check" not in doctor and "get_embedder" not in doctor


# --- E: a trial over chosen cases -------------------------------------------------------------


TRIAL = "trial-gpt"
CASE_IDS = ("attack-llmail-01d16d4e4af9", "benign-llmailfp-0", "attack-prag-hotpotqa-x")


def test_a_trial_passes_its_case_ids_to_every_runner_and_logs_them(bench: Bench) -> None:
    bench.runner_outcomes({}, {})  # each config finishes clean: no retry pass
    assert run_campaign(bench.ctx, opts(run=TRIAL, case_ids=CASE_IDS)) == 0

    runners = [cmd for kind, cmd in bench.host.events if kind == "run" and "live.run" in label(cmd)]
    assert len(runners) == 2
    for cmd in runners:
        assert cmd[cmd.index("--case-ids") + 1] == ",".join(CASE_IDS)
    configs = [s for s in bench.kit_log(TRIAL) if s["step"] == "config"]
    assert {tuple(s["case_ids"]) for s in configs} == {CASE_IDS}


def test_a_full_run_passes_no_case_ids(bench: Bench) -> None:
    assert run_campaign(bench.ctx, opts()) == 0
    assert all("--case-ids" not in cmd for kind, cmd in bench.host.events if kind == "run")


def test_a_trial_over_chosen_cases_must_use_a_trial_folder(bench: Bench) -> None:
    assert run_campaign(bench.ctx, opts(run="2026-10-02-gpt4omini-live", case_ids=CASE_IDS)) == 2

    assert "a trial folder is never a result" in "\n".join(bench.err)
    assert bench.host.events == [] and bench.embedding.requests == []


def test_a_finished_trial_is_not_a_finished_run_and_the_other_way_round() -> None:
    record = {
        "step": "config",
        "config": "C0",
        "status": "ok",
        "model_profile": "gpt-4o-mini",
        "limit": None,
        "case_ids": list(CASE_IDS),
        "counts": {"selected": 3, "ok": 3, "error": 0, "skipped_already_recorded": 0},
    }
    assert finished_configs([record], "gpt-4o-mini", None, CASE_IDS) == {"C0"}
    assert finished_configs([record], "gpt-4o-mini", None) == set()
    full = {**record, "case_ids": None}
    assert finished_configs([full], "gpt-4o-mini", None) == {"C0"}
    assert finished_configs([full], "gpt-4o-mini", None, CASE_IDS) == set()


def test_the_resume_command_of_a_trial_keeps_its_case_ids() -> None:
    options = RunOptions(model_profile="gpt-4o-mini", run=TRIAL, configs=("C0",), case_ids=CASE_IDS)
    make, module = resume_commands(options)
    assert f"CASE_IDS={','.join(CASE_IDS)}" in make
    assert f"--case-ids {','.join(CASE_IDS)}" in module


def test_the_command_line_takes_a_comma_list_of_case_ids() -> None:
    args = campaign.parse_args(
        ["run", "--model-profile", "gpt-4o-mini", "--run", TRIAL, "--case-ids", "a, b,c"]
    )
    assert args.case_ids == ("a", "b", "c")


def test_make_bench_run_passes_case_ids_as_an_argument_only() -> None:
    printed = subprocess.run(
        ["make", "--no-print-directory", "-n", "bench-run", "MODEL=gpt-4o-mini", f"RUN={TRIAL}",
         "CASE_IDS=a,b"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert "--case-ids a,b" in printed
    leaked = subprocess.run(
        ["make", "-s", "--eval", 'print-env: ; @env | grep "^CASE_IDS=" || true', "print-env",
         "CASE_IDS=a"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert leaked == ""


TRIAL_GUIDES = ("docs/BENCHMARK.md", "README.md", "docs/benchmark-windows-native.md")


def _trial_commands(text: str) -> list[dict[str, Any]]:
    """Each trial command of a guide (the kit's make target or its module), its words parsed."""
    found = []
    for line in text.splitlines():
        ids = re.search(r"(?:CASE_IDS=|--case-ids )([\w,.-]+)", line)
        if ids is None or not ("make bench-run" in line or "campaign run" in line):
            continue
        configs = re.search(r"(?:CONFIGS=|--configs )([\w,]+)", line)
        run = re.search(r"(?:\bRUN=|--run )([\w.-]+)", line)
        found.append(
            {
                "ids": ids.group(1).split(","),
                "configs": configs.group(1).split(",") if configs else None,
                "run": run.group(1) if run else "",
            }
        )
    return found


@pytest.mark.parametrize("guide", TRIAL_GUIDES)
def test_the_guides_trial_command_covers_an_attack_a_benign_and_a_rag_case(guide: str) -> None:
    # The ids live only in the guides; a drift from the pinned cases must fail here, not on the
    # teammate's machine. Every config of the trial must run each id (the kit refuses one it does
    # not), and exactly one id is a RAG case with knowledge documents to embed.
    from evaluation.mailguard_bench.case_adapter import EvalCase
    from evaluation.mailguard_bench.cases import DEFAULT_CASE_DIR, load_case_set
    from evaluation.mailguard_bench.runner import config_case_ids

    commands = _trial_commands((REPO / guide).read_text("utf-8"))
    assert commands, f"{guide} names no trial command"
    loaded = load_case_set(DEFAULT_CASE_DIR)
    for command in commands:
        assert command["run"].startswith("trial")
        assert command["configs"], "a trial names its configs"
        for config in command["configs"]:
            assert set(command["ids"]) <= set(config_case_ids(loaded.manifest, config, "v2"))
        cases = [EvalCase.from_dict(loaded.cases[i]) for i in command["ids"]]
        kinds = sorted((case.kind, bool(case.kb_docs)) for case in cases)
        assert kinds == [("attack", False), ("attack", True), ("benign", False)]


def test_the_guides_name_the_same_trial_cases() -> None:
    ids = {
        tuple(command["ids"])
        for guide in TRIAL_GUIDES
        for command in _trial_commands((REPO / guide).read_text("utf-8"))
    }
    assert len(ids) == 1


# --- F: a run stopped by a limit resumes after another model's run ----------------------------


def test_a_limit_stop_resumes_after_another_models_run_with_the_same_stack_env(
    bench: Bench,
) -> None:
    # gpt-4o-mini stops on a daily cap in C0; Qwen2.5-7B runs on OpenRouter in its own RUN; the
    # gpt-4o-mini command runs again. The stack env is rendered for gpt-4o-mini again, exactly as
    # the first time, the containers get it, and the stopped config resumes with its error rows.
    env_file = bench.repo / ".env"
    env_file.write_text(
        env_file.read_text("utf-8") + "BENCH_OPENROUTER_API_KEY=sk-or-000\n", "utf-8"
    )
    stack = bench.repo / ".env.stack"
    gpt = opts(run="2026-10-02-gpt4omini-live", configs=("C0", "C3"))
    qwen = opts(
        model_profile="qwen2.5-7b-openrouter",
        run="2026-10-02-qwen25-openrouter-live",
        configs=("C0", "C3"),
    )

    bench.runner_outcomes({"ok": 1, "error": 1})
    bench.host.exits["live.run C0"] = ROUTE_STOP_EXIT
    assert run_campaign(bench.ctx, gpt) == 1
    first_render = stack.read_text("utf-8")
    first_runner = _runner_commands(bench, "C0")[-1]
    assert "qwen" not in first_render

    del bench.host.exits["live.run C0"]
    bench.runner_outcomes({}, {})
    assert run_campaign(bench.ctx, qwen) == 0
    assert "qwen/qwen-2.5-7b-instruct" in stack.read_text("utf-8")

    bench.host.events.clear()
    bench.runner_outcomes({"ok": 2, "skipped": 0}, {})
    assert run_campaign(bench.ctx, gpt) == 0

    assert stack.read_text("utf-8") == first_render  # the same settings, so the same fingerprint
    assert sequence(bench.host)[0] == STACK_UP  # the containers are recreated with them
    assert _runner_commands(bench, "C0") == [first_runner]  # the same command, --retry-errors in
    assert "--retry-errors" in first_runner
    gpt_log = [s for s in bench.kit_log(gpt.run) if s["step"] == "config"]
    assert [(s["config"], s["status"]) for s in gpt_log] == [
        ("C0", "stopped"),
        ("C0", "ok"),
        ("C3", "ok"),
    ]
    assert OPENAI_KEY not in "\n".join(bench.out + bench.err)


def _runner_commands(bench: Bench, config: str) -> list[list[str]]:
    return [
        cmd
        for kind, cmd in bench.host.events
        if kind == "run" and label(cmd) == f"live.run {config}"
    ]
