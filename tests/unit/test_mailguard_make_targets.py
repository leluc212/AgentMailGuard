"""The benchmark's Make targets are owner-run and never part of ``make ci`` (task 7.19; R24.5)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MAKEFILE = (REPO / "Makefile").read_text(encoding="utf-8")


def targets() -> set[str]:
    return set(re.findall(r"^([A-Za-z0-9_.-]+):(?!=)", MAKEFILE, re.MULTILINE))


def recipe(target: str) -> str:
    match = re.search(rf"^{re.escape(target)}:.*\n((?:\t.*\n?)+)", MAKEFILE, re.MULTILINE)
    assert match, f"no recipe for {target}"
    return match.group(1)


def ci_prerequisites() -> list[str]:
    match = re.search(r"^ci:(.*)$", MAKEFILE, re.MULTILINE)
    assert match
    return match.group(1).split()


def phony() -> list[str]:
    match = re.search(r"^\.PHONY:(.*)$", MAKEFILE, re.MULTILINE)
    assert match
    return match.group(1).split()


def test_no_mailguard_target_runs_in_ci() -> None:
    assert not [t for t in ci_prerequisites() if t.startswith("mailguard")]


def test_report_target_runs_the_module_from_the_repo_root_with_the_worktree() -> None:
    assert "mailguard-report" in targets() and "mailguard-report" in phony()
    body = recipe("mailguard-report")
    # `python -m` from the repo root keeps rag-email's `evaluation` package ahead of
    # AgentMailGuard's (both are top-level); --with-editable leaves uv.lock untouched.
    assert "--with-editable $(MAILGUARD_DIR)" in MAKEFILE
    assert "-m evaluation.mailguard_bench.report" in body
    assert 'test -n "$(RUN)"' in body


def test_analyses_target_scores_then_analyses_then_reports() -> None:
    assert "mailguard-analyses" in targets() and "mailguard-analyses" in phony()
    steps = re.findall(r"-m (evaluation\.mailguard_bench\.\w+)", recipe("mailguard-analyses"))
    assert steps == [
        "evaluation.mailguard_bench.report",
        "evaluation.mailguard_bench.analyses",
        "evaluation.mailguard_bench.report",
    ]


RUNBOOK = (REPO / "docs" / "demo-runbook.md").read_text(encoding="utf-8")


def test_every_benchmark_command_in_the_runbook_is_a_make_target() -> None:
    named = set(re.findall(r"make (mailguard-[a-z0-9-]+)", RUNBOOK))
    assert {"mailguard-bench", "mailguard-report", "mailguard-analyses"} <= named
    assert named <= targets(), f"runbook names unknown targets: {sorted(named - targets())}"


def _make(*args: str) -> str:
    return subprocess.run(
        ["make", "--no-print-directory", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_the_worker_count_reaches_the_runner_as_an_argument_only() -> None:
    # demo-runbook §9.8 passes CONCURRENCY=2 on the make command line. Make exports such a
    # variable to every recipe, and AppSettings reads CONCURRENCY as its `concurrency` group,
    # so every run stopped at "1 validation error for AppSettings" before its first case.
    leaked = _make(
        "-s",
        "--eval",
        'print-env: ; @env | grep "^CONCURRENCY=" || true',
        "print-env",
        "CONCURRENCY=2",
    )
    assert leaked == ""
    dry_run = _make("-n", "mailguard-bench", "RUN=r", "CONFIG=C0", "CONCURRENCY=2")
    assert "--concurrency 2" in dry_run


ABLATION_CONFIGS = ("C3-L1", "C3-L2", "C3-L3", "C3-L3B", "C3-L4", "C3-L5")
V1_CONFIGS = ("C0", "C0T", "C1", "C2", "C3", *ABLATION_CONFIGS)
V2_CONFIGS = ("C0", "C0T", "C1", "C2", "C3", "C4", "C5", "C6", "C7")


def test_the_bench_target_accepts_the_v1_configs_under_scheme_v1() -> None:
    # the layer ablation exists only in scheme v1, the published one (ADR-0012 decision 11)
    for config in V1_CONFIGS:
        dry_run = _make(
            "-n", "mailguard-bench", "RUN=x", f"CONFIG={config}", "SCHEME=v1", "MODEL=gpt-4o-mini"
        )
        assert f"--config {config} " in dry_run
        assert "--scheme v1" in dry_run
        assert "--model-profile gpt-4o-mini" in dry_run


def test_the_bench_target_defaults_to_scheme_v2_and_accepts_its_configs() -> None:
    for config in V2_CONFIGS:
        dry_run = _make("-n", "mailguard-bench", "RUN=x", f"CONFIG={config}", "MODEL=gpt-4o-mini")
        assert f"--config {config} " in dry_run
        assert "--scheme v2" in dry_run
        assert "--model-profile gpt-4o-mini" in dry_run


def test_the_scheme_reaches_the_runner_as_an_argument_only() -> None:
    # like CONCURRENCY: Make would export a command-line SCHEME to every recipe
    leaked = _make(
        "-s", "--eval", 'print-env: ; @env | grep "^SCHEME=" || true', "print-env", "SCHEME=v1"
    )
    assert leaked == ""


def _config_check_exit(config: str, scheme: str = "v2") -> int:
    """Exit code of the recipe's first line (the CONFIG check), run alone; no run starts."""
    first = recipe("mailguard-bench").splitlines()[0].lstrip("@\t ")
    command = first.replace("$(CONFIG)", config).replace("$(SCHEME)", scheme)
    return subprocess.run(["bash", "-c", command], capture_output=True, check=False).returncode


def test_the_config_check_lets_each_schemes_configs_through_and_only_those() -> None:
    for config in V2_CONFIGS:
        assert _config_check_exit(config, "v2") == 0, config
    for config in ("C3-L1", "C3-L6", "C3-l1", "C3-", "C8", "C0t", ""):
        assert _config_check_exit(config, "v2") == 2, config
    for config in V1_CONFIGS:
        assert _config_check_exit(config, "v1") == 0, config
    for config in ("C3-L6", "C3-l1", "C3-", "C4", "C5", "C6", "C7", ""):
        assert _config_check_exit(config, "v1") == 2, config
    for scheme in ("v3", "", "V2"):
        assert _config_check_exit("C0", scheme) == 2, scheme


def _usage(*args: str) -> str:
    result = subprocess.run(
        ["make", "--no-print-directory", "mailguard-bench", "RUN=x", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    return result.stdout + result.stderr


def test_the_bench_target_rejects_other_configs_and_its_usage_names_every_config() -> None:
    for args in (["CONFIG=C3-L1"], ["CONFIG=C4", "SCHEME=v1"], ["CONFIG=C8"], ["SCHEME=v3"]):
        usage = _usage(*args)
        assert usage.startswith("usage: make mailguard-bench")
        for config in (*V2_CONFIGS, *ABLATION_CONFIGS):
            assert config in usage, (args, config)
        assert "SCHEME=v1" in usage and "SCHEME=v2" in usage
        assert "python -m evaluation" not in usage  # rejected before anything runs


def _runbook_part(start: str, end: str | None) -> str:
    text = RUNBOOK[RUNBOOK.index(start) :]
    return text if end is None else text[: text.index(end)]


def test_every_v1_command_of_the_runbook_says_scheme_v1() -> None:
    # the default scheme is v2 now, so a v1 command that left the argument out would run v2
    v1 = _runbook_part("## 9. AgentMailGuard benchmark", "### 9.9 ")
    blocks = re.findall(r"```bash\n(.*?)```", v1, re.DOTALL)
    commands = [
        line for block in blocks for line in block.splitlines() if "make mailguard-bench " in line
    ]
    assert len(commands) >= 8
    assert [line for line in commands if "SCHEME=v1" not in line] == []


def test_the_v2_run_loop_of_the_runbook_runs_every_v2_config_twice() -> None:
    v2 = _runbook_part("### 9.9 ", "## Appendix A")
    loop = "for c in C0 C0T C1 C2 C3 C4 C5 C6 C7; do run_config $c; done"
    assert v2.count(loop) == 2  # the first pass and the retry pass
    assert "--scheme v1" in v2 or "SCHEME=v1" in v2  # how a v1 name is run live is said
    assert "make mailguard-report RUN=$RUN" in v2  # v2 has no no-API analyses
    assert "81df5d07" in RUNBOOK and "SCHEME=v1" in RUNBOOK  # how v1 is reproduced
