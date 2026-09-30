"""The kit's other commands: setup, report, package and the command line (task 7.23).

Same fake machine as the orchestrator's tests (``mailguard_kit_fixtures``); the guard worktree is
a real temporary git repository so ``guard_env.require_pinned_worktree`` runs for real, and the
R6b module ``kit.pinned`` is faked at its import boundary (it is written in another package).
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
import zipfile
from pathlib import Path
from typing import Any

import pytest

from evaluation.mailguard_bench.kit import campaign
from evaluation.mailguard_bench.kit.campaign import (
    KitContext,
    run_package,
    run_reports,
    run_setup,
)
from tests.unit.mailguard_kit_fixtures import (  # noqa: F401  (bench_fixture is the `bench` fixture)
    COPY_MODEL,
    GEMINI_KEY,
    OPENAI_KEY,
    REPORTS,
    REPORTS_V1,
    RUN,
    Bench,
    bench_fixture,
    rerank_dir,
    rerank_marker,
    sequence,
)

PINNED = "evaluation.mailguard_bench.kit.pinned"
COPY_MODEL_ARGS = ["docker", "compose", "cp"]


def _git(path: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def guard(tmp_path: Path) -> tuple[Path, str]:
    """A clean git worktree with one commit, standing in for AgentMailGuard at its pin."""
    root = tmp_path / "guard"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "README").write_text("guard\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-q", "-m", "guard")
    return root, _git(root, "rev-parse", "HEAD")


def with_guard(bench: Bench, guard: tuple[Path, str], **extra: str) -> KitContext:
    root, commit = guard
    environ = {
        "MAILGUARD_DIR": str(root),
        "MAILGUARD_COMMIT": commit,
        "MAILGUARD_ARTIFACTS": str(root / "artifacts"),
        **extra,
    }
    return KitContext(**{**bench.ctx.__dict__, "environ": environ})


def fake_pinned(monkeypatch: pytest.MonkeyPatch, problems: list[str] | None = None) -> list[Any]:
    calls: list[Any] = []
    module = types.ModuleType(PINNED)

    def verify(root: Path) -> list[str]:
        calls.append(root)
        return problems or []

    module.verify = verify  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, PINNED, module)
    return calls


# --- setup ------------------------------------------------------------------------------------


def test_setup_checks_the_guard_and_the_pins_then_smokes_then_brings_the_stack_up(
    bench: Bench, guard: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_pinned(monkeypatch)
    ctx = with_guard(bench, guard)
    assert run_setup(ctx) == 0
    assert calls == [bench.repo]  # the pinned inputs of THIS checkout
    assert sequence(bench.host) == [
        "guard_smoke",
        "docker compose build",
        "docker compose up",
        COPY_MODEL,
    ]
    build, up, *_ = (p for k, p in bench.host.events if k == "run" and p[0] == "docker")
    # the images say which commit they were built from: the run refuses any other (live.run)
    assert build == ["docker", "compose", "build", "--build-arg", f"GIT_COMMIT={bench.host.head}"]
    assert up == ["docker", "compose", "up", "-d"]  # built just now: nothing builds again
    smoke = next(p for k, p in bench.host.events if k == "run" and p[0] != "docker")
    assert smoke == ["py", "-m", "evaluation.mailguard_bench.guard_smoke"]  # no --live-probe
    ps = [
        p for k, p in bench.host.events if k == "capture" and p[:3] == ["docker", "compose", "ps"]
    ]
    assert ps and "api" not in ps[0]  # the whole project, not four services


def test_setup_builds_nothing_when_the_checkouts_commit_cannot_be_read(
    bench: Bench, guard: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_pinned(monkeypatch)
    bench.host.head = ""  # `git rev-parse HEAD` printed nothing: no commit, or not a checkout
    assert run_setup(with_guard(bench, guard)) == 1
    assert any("commit" in line and "git rev-parse HEAD" in line for line in bench.err)
    assert not any(k == "run" and p[0] == "docker" for k, p in bench.host.events)


def test_setup_copies_the_reranker_model_out_of_the_image_once_the_stack_is_healthy(
    bench: Bench, guard: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_pinned(monkeypatch)
    assert run_setup(with_guard(bench, guard)) == 0
    events = bench.host.events
    copied_at = next(
        i for i, (kind, cmd) in enumerate(events) if kind == "run" and cmd[:3] == COPY_MODEL_ARGS
    )
    polled_at = max(
        i
        for i, (kind, cmd) in enumerate(events)
        if kind == "capture" and cmd[:2] == ["docker", "inspect"] and cmd[3] != "{{.Image}}"
    )
    assert polled_at < copied_at  # the health wait came first: the container exists by now
    assert (rerank_dir(bench) / "models--fake--cross-encoder").is_dir()
    assert rerank_marker(bench).read_text(encoding="utf-8").strip() == bench.host.image_id
    assert any("reranker model copied from the ai-worker image" in line for line in bench.out)
    assert "Next: make bench-run" in bench.out[-1]  # the hint stays the last word


def test_setup_fails_when_the_reranker_model_cannot_be_copied(
    bench: Bench, guard: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_pinned(monkeypatch)
    bench.host.exits[COPY_MODEL] = 1
    assert run_setup(with_guard(bench, guard)) == 1
    command = f"docker compose cp ai-worker:/app/.cache/reranker {rerank_dir(bench)}"
    assert f"FAIL {command} exited 1" in "\n".join(bench.err)
    assert not any("Next: make bench-run" in line for line in bench.out)
    assert not rerank_marker(bench).exists()


def test_setup_run_again_copies_only_when_the_image_was_rebuilt(
    bench: Bench, guard: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_pinned(monkeypatch)
    ctx = with_guard(bench, guard)
    assert run_setup(ctx) == 0
    bench.host.events.clear()
    assert run_setup(ctx) == 0  # the same image: the copy is current
    assert COPY_MODEL not in sequence(bench.host)
    assert any("already the ai-worker image's" in line for line in bench.out)
    bench.host.events.clear()
    bench.host.image_id = "sha256:" + "2" * 64  # `docker compose up --build` made a new image
    assert run_setup(ctx) == 0
    assert COPY_MODEL in sequence(bench.host)
    assert rerank_marker(bench).read_text(encoding="utf-8").strip() == bench.host.image_id


def test_setup_stops_when_the_guard_is_not_at_the_pin_before_anything_else(
    bench: Bench, guard: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_pinned(monkeypatch)
    ctx = with_guard(bench, guard, MAILGUARD_COMMIT="0" * 40)
    assert run_setup(ctx) == 1
    assert bench.host.events == []
    assert any("pinned commit" in line for line in bench.err)


def test_setup_refuses_a_dirty_guard_worktree(
    bench: Bench, guard: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_pinned(monkeypatch)
    (guard[0] / "README").write_text("changed\n", encoding="utf-8")
    assert run_setup(with_guard(bench, guard)) == 1
    assert any("uncommitted" in line for line in bench.err)


def test_setup_needs_the_make_overlay_variables(bench: Bench) -> None:
    assert run_setup(bench.ctx) == 1  # MAILGUARD_COMMIT and MAILGUARD_ARTIFACTS are not set
    assert any("MAILGUARD_COMMIT" in line for line in bench.err)


def test_setup_lists_every_pinned_input_problem_and_runs_nothing(
    bench: Bench, guard: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_pinned(monkeypatch, ["cases.jsonl sha256 differs", "SHA256SUMS is missing"])
    assert run_setup(with_guard(bench, guard)) == 1
    assert bench.host.events == []
    assert (
        "FAIL cases.jsonl sha256 differs" in bench.err and "FAIL SHA256SUMS is missing" in bench.err
    )


def test_setup_says_so_when_the_pinned_check_is_not_installed(
    bench: Bench, guard: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, PINNED, None)  # an import of it raises ImportError
    assert run_setup(with_guard(bench, guard)) == 1
    assert any(PINNED in line for line in bench.err)


def test_setup_does_not_build_the_stack_when_the_smoke_fails(
    bench: Bench, guard: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_pinned(monkeypatch)
    bench.host.exits["guard_smoke"] = 1
    assert run_setup(with_guard(bench, guard)) == 1
    assert sequence(bench.host) == ["guard_smoke"]


def test_setup_waits_for_every_container_and_a_one_shot_one_that_exited_cleanly_is_fine(
    bench: Bench, guard: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_pinned(monkeypatch)
    seen = {"polls": 0}

    def health(ids: list[str]) -> list[str]:
        seen["polls"] += 1
        late = "starting" if seen["polls"] < 3 else "healthy"
        return [f"/{ids[0]}|running|{late}|0", "/init|exited||0", "/grafana|running||0"]

    bench.host.health = health
    assert run_setup(with_guard(bench, guard)) == 0
    assert seen["polls"] == 3


def test_setup_fails_when_a_container_never_gets_healthy(
    bench: Bench, guard: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_pinned(monkeypatch)
    bench.host.health = lambda ids: [f"/{ids[0]}|running|unhealthy|0"]
    assert run_setup(with_guard(bench, guard)) == 1
    assert any("not healthy" in line and "unhealthy" in line for line in bench.err)


def test_setup_fails_when_a_one_shot_container_exited_with_an_error(
    bench: Bench, guard: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_pinned(monkeypatch)
    bench.host.health = lambda ids: ["/init|exited||1"]
    assert run_setup(with_guard(bench, guard)) == 1


# --- report -----------------------------------------------------------------------------------


def _finished_run(bench: Bench, scheme: str | None = "v2") -> Path:
    """A run folder whose config meta records ``scheme`` (None: a v1 meta, which has no key)."""
    run_dir = bench.results_root / RUN
    (run_dir / "raw").mkdir(parents=True)
    meta = {"invocations": []} if scheme is None else {"scheme": scheme, "invocations": []}
    (run_dir / "raw" / "C0.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return run_dir


def test_report_builds_the_reports_of_a_finished_run(bench: Bench) -> None:
    _finished_run(bench)
    assert run_reports(bench.ctx, RUN) == 0
    assert sequence(bench.host) == REPORTS
    assert [r["step"] for r in bench.kit_log()] == ["reports"]


def test_report_with_a_reader_adds_the_meaning_column_between_two_builds(bench: Bench) -> None:
    _finished_run(bench)
    assert run_reports(bench.ctx, RUN, "gemini-2.5-flash") == 0
    assert sequence(bench.host) == [*REPORTS, "meaning", *REPORTS]


def test_report_refuses_a_benchmarked_reader_and_a_missing_run(bench: Bench) -> None:
    _finished_run(bench)
    assert run_reports(bench.ctx, RUN, "llama3.1:8b") == 2
    assert run_reports(bench.ctx, "no-such-run") == 2
    assert bench.host.events == []


def test_report_of_a_v1_run_keeps_report_analyses_report(bench: Bench) -> None:
    _finished_run(bench, scheme=None)
    assert run_reports(bench.ctx, RUN) == 0
    assert sequence(bench.host) == REPORTS_V1


def test_report_of_a_v2_run_never_calls_the_v1_only_analyses(bench: Bench) -> None:
    """The kit smoke of 2026-10-01 stopped here: analyses refuses a scheme v2 folder."""
    _finished_run(bench, scheme="v2")
    assert run_reports(bench.ctx, RUN, "gemini-2.5-flash") == 0
    assert "analyses" not in sequence(bench.host)
    assert sequence(bench.host) == ["report", "meaning", "report"]


def test_report_stops_at_the_first_failing_step(bench: Bench) -> None:
    _finished_run(bench, scheme=None)
    bench.host.exits["analyses"] = 1
    assert run_reports(bench.ctx, RUN) == 1
    assert sequence(bench.host) == ["report", "analyses"]


# --- package ----------------------------------------------------------------------------------


def _populate(bench: Bench) -> Path:
    run_dir = bench.results_root / RUN
    files = {
        "manifest.json": "{}",
        "report.md": "# r",
        "kit-log.jsonl": '{"step": "stack"}\n',
        "raw/C0.jsonl": '{"case": 1}\n',
        "raw/C0.meta.json": "{}",
        "raw/guard-worker.C3.log": "log",
        "analysis/x.jsonl": "{}",
        # None of these may travel; a stray copy of the stack env is the danger.
        ".env": f"K={OPENAI_KEY}\n",
        "raw/.env.stack": f"EMBEDDING__API_KEY={GEMINI_KEY}\n",
        "raw/.kit-stamp.C3": "",
    }
    for name, text in files.items():
        path = run_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return run_dir


def test_the_zip_holds_the_run_folder_with_raw_and_the_kit_log_and_no_env_file(
    bench: Bench,
) -> None:
    _populate(bench)
    assert run_package(bench.ctx, RUN) == 0
    archive = bench.repo / f"bench-results-{RUN}.zip"
    names = set(zipfile.ZipFile(archive).namelist())
    assert names == {
        f"{RUN}/manifest.json",
        f"{RUN}/report.md",
        f"{RUN}/kit-log.jsonl",
        f"{RUN}/raw/C0.jsonl",
        f"{RUN}/raw/C0.meta.json",
        f"{RUN}/raw/guard-worker.C3.log",
        f"{RUN}/analysis/x.jsonl",
    }
    blob = b"".join(zipfile.ZipFile(archive).read(n) for n in names)
    assert OPENAI_KEY.encode() not in blob and GEMINI_KEY.encode() not in blob


def test_the_zip_is_refused_when_a_key_is_in_any_file(bench: Bench) -> None:
    run_dir = _populate(bench)
    (run_dir / "raw" / "C0.jsonl").write_text(f'{{"err": "bad key {OPENAI_KEY}"}}\n', "utf-8")
    assert run_package(bench.ctx, RUN) == 1
    message = "\n".join(bench.err)
    assert "raw/C0.jsonl" in message and OPENAI_KEY not in message
    assert not (bench.repo / f"bench-results-{RUN}.zip").exists()


@pytest.mark.parametrize(
    "name", ["POSTGRES_PASSWORD", "RABBITMQ_TOKEN", "APP_SECRET", "SIGNING_KEY", "X_API_KEY"]
)
def test_the_zip_is_refused_for_any_secret_shaped_setting_not_only_api_keys(
    bench: Bench, name: str
) -> None:
    value = "correct-horse-battery"
    run_dir = _populate(bench)
    (run_dir / "raw" / "C0.jsonl").write_text(f'{{"err": "{value}"}}\n', "utf-8")
    ctx = KitContext(**{**bench.ctx.__dict__, "environ": {**bench.ctx.environ, name: value}})
    assert run_package(ctx, RUN) == 1
    message = "\n".join(bench.err)
    assert "raw/C0.jsonl" in message and value not in message
    assert not (bench.repo / f"bench-results-{RUN}.zip").exists()


def test_the_package_prints_how_to_commit_the_tracked_outputs_to_a_bench_branch(
    bench: Bench,
) -> None:
    _populate(bench)
    assert run_package(bench.ctx, RUN) == 0
    printed = "\n".join(bench.out)
    base = f"evaluation/results/mailguard_bench/{RUN}"
    assert f"git switch -c bench/{RUN}" in printed
    assert f"git push -u origin bench/{RUN}" in printed
    add = next(line for line in bench.out if "git add" in line)
    for tracked in ("manifest.json", "report.md", "kit-log.jsonl", "analysis"):
        assert f"{base}/{tracked}" in add
    assert f"{base}/raw" not in add  # raw/ holds the full attack emails: zip only
    assert "metrics.csv" not in add  # it does not exist in this run
    commit = next(line for line in bench.out if "git commit" in line)
    assert "[task 7.20]" in commit  # results belong to the v2 benchmark task, not the kit's


def test_package_of_a_missing_run_fails(bench: Bench) -> None:
    assert run_package(bench.ctx, "no-such-run") == 1


def test_the_zip_can_go_elsewhere(bench: Bench, tmp_path: Path) -> None:
    _populate(bench)
    target = tmp_path / "out" / "x.zip"
    assert run_package(bench.ctx, RUN, target) == 0
    assert target.is_file()


# --- the command line -------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in list(os.environ):
        if name.startswith(campaign.EXPORTED_PREFIXES) or name in campaign.EXPORTED_NAMES:
            monkeypatch.delenv(name)


def test_main_dry_run_goes_through_the_real_wiring_and_prints_commands(
    clean_env: None, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = campaign.main(
        [
            "run",
            "--model-profile",
            "qwen2.5-7b",
            "--run",
            f"dry-{tmp_path.name}",
            "--configs",
            "C0,C3",
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "would run: " in out and "evaluation.mailguard_bench.live.run --config C0" in out
    assert "--model-profile qwen2.5-7b" in out


def test_main_package_of_a_missing_run_is_exit_1(
    clean_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert campaign.main(["package", "--run", "definitely-not-a-run"]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_main_turns_sigterm_into_the_same_clean_stop_as_ctrl_c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import signal

    if not hasattr(signal, "SIGTERM"):
        pytest.skip("no SIGTERM")

    def fake_run(ctx: KitContext, options: Any) -> int:
        os.kill(os.getpid(), signal.SIGTERM)
        return 0

    monkeypatch.setattr(campaign, "run_campaign", fake_run)
    before = signal.getsignal(signal.SIGTERM)
    code = campaign.main(["run", "--model-profile", "qwen2.5-7b", "--run", "x", "--dry-run"])
    assert code == 130
    assert signal.getsignal(signal.SIGTERM) == before  # restored
