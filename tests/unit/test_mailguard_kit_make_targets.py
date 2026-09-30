"""The kit's Make targets (task 7.23): same overlay as the ``mailguard-*`` targets, never in CI.

Only ``make -n`` (print, do not run) and the targets' own argument checks are run here: no
docker, no model call. The recipes are pure module calls, so what they print is what they do.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MAKEFILE = (REPO / "Makefile").read_text(encoding="utf-8")
TARGETS = ("bench-doctor", "bench-setup", "bench-run", "bench-report", "bench-package")
CAMPAIGN = "-m evaluation.mailguard_bench.kit.campaign"


def make(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "--no-print-directory", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=check,
    )


def dry(*args: str) -> str:
    return make("-n", *args).stdout


def test_every_kit_target_is_declared_phony_and_none_runs_in_ci() -> None:
    phony = {
        name
        for line in re.findall(r"^\.PHONY:(.*)$", MAKEFILE, re.MULTILINE)
        for name in line.split()
    }
    ci = re.search(r"^ci:(.*)$", MAKEFILE, re.MULTILINE)
    assert ci
    for target in TARGETS:
        assert target in phony, target
        assert target not in ci.group(1).split()


def test_every_kit_target_has_a_help_line() -> None:
    help_text = make("help").stdout
    for target in TARGETS:
        assert re.search(rf"^\s+{target}\b", help_text, re.MULTILINE), target


def test_the_targets_run_under_the_overlay_of_the_mailguard_targets() -> None:
    for target, args in (
        ("bench-doctor", []),
        ("bench-setup", []),
        ("bench-run", ["MODEL=gpt-4o-mini", "RUN=x"]),
        ("bench-report", ["RUN=x"]),
        ("bench-package", ["RUN=x"]),
    ):
        printed = dry(target, *args)
        assert "--with-editable" in printed, target
        assert "MAILGUARD_COMMIT=" in printed and "MAILGUARD_ARTIFACTS=" in printed, target


def test_doctor_and_setup_run_their_modules() -> None:
    assert "python -m evaluation.mailguard_bench.kit.doctor" in dry("bench-doctor")
    assert f"python {CAMPAIGN} setup" in dry("bench-setup")


def test_bench_run_needs_a_run_id_so_a_resume_after_midnight_never_starts_a_new_run() -> None:
    # A run id with today's date in it, made by default, would change at midnight: a three-model
    # campaign spans days, and a smoke run without RUN would land in the real run's folder.
    result = make("bench-run", "MODEL=qwen2.5-7b", check=False)
    assert result.returncode == 2
    text = result.stdout + result.stderr
    assert text.startswith("usage: make bench-run")
    assert "RUN=<id>" in text
    assert "kit.campaign" not in text


def test_bench_run_passes_the_model_and_the_run_to_the_kit() -> None:
    printed = dry("bench-run", "MODEL=qwen2.5-7b", "RUN=r1")
    assert f"python {CAMPAIGN} run" in printed
    assert "--model-profile qwen2.5-7b" in printed
    assert "--run r1" in printed


def test_bench_run_refuses_a_reader_and_points_to_bench_report() -> None:
    result = make("bench-run", "MODEL=gpt-4o-mini", "RUN=r", "READER=some-reader", check=False)
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert "bench-report" in text
    assert "kit.campaign" not in text


def test_dry_run_is_on_for_1_yes_true_and_off_for_anything_else() -> None:
    for on in ("1", "yes", "true"):
        assert "--dry-run" in dry("bench-run", "MODEL=m", "RUN=r", f"DRY_RUN={on}"), on
    for off in ("0", "false", "no", ""):
        assert "--dry-run" not in dry("bench-run", "MODEL=m", "RUN=r", f"DRY_RUN={off}"), off


def test_bench_run_leaves_the_configs_to_the_kit_unless_given() -> None:
    printed = dry("bench-run", "MODEL=gpt-4o-mini", "RUN=r")
    for flag in ("--configs", "--limit", "--concurrency", "--reader", "--dry-run"):
        assert flag not in printed, flag  # the kit's one constant is the default list


def test_bench_run_passes_what_it_is_given() -> None:
    printed = dry(
        "bench-run",
        "MODEL=gpt-4o-mini",
        "RUN=2026-10-02-gpt4omini-live",
        "CONFIGS=C0,C3",
        "LIMIT=5",
        "CONCURRENCY=2",
        "DRY_RUN=1",
    )
    assert "--run 2026-10-02-gpt4omini-live" in printed
    assert "--configs C0,C3" in printed
    assert "--limit 5" in printed
    assert "--concurrency 2" in printed
    assert "--dry-run" in printed


def test_concurrency_reaches_the_kit_as_an_argument_only_never_as_an_environment_variable() -> None:
    # AppSettings reads CONCURRENCY as its `concurrency` settings group (the v1 target's test).
    leaked = make(
        "-s",
        "--eval",
        'print-env: ; @env | grep "^CONCURRENCY=" || true',
        "print-env",
        "CONCURRENCY=2",
    ).stdout
    assert leaked == ""


def test_bench_run_without_a_model_fails_with_usage_before_anything_runs() -> None:
    result = make("bench-run", check=False)
    assert result.returncode == 2
    assert (result.stdout + result.stderr).startswith("usage: make bench-run")
    assert "kit.campaign" not in result.stdout + result.stderr


def test_bench_report_and_package_need_a_run() -> None:
    for target in ("bench-report", "bench-package"):
        result = make(target, check=False)
        assert result.returncode != 0
        assert "RUN" in result.stdout + result.stderr


def test_bench_report_and_package_run_the_kit_with_the_run() -> None:
    assert f"python {CAMPAIGN} report --run x" in dry("bench-report", "RUN=x")
    assert "--reader r" in dry("bench-report", "RUN=x", "READER=r")
    assert f"python {CAMPAIGN} package --run x" in dry("bench-package", "RUN=x")


def test_the_package_zip_at_the_repo_root_is_git_ignored() -> None:
    # It holds raw/ (the full attack emails and drafts), which stays out of git.
    done = subprocess.run(
        ["git", "check-ignore", "--quiet", "bench-results-2026-10-02-x-live.zip"],
        cwd=REPO,
        check=False,
    )
    assert done.returncode == 0
