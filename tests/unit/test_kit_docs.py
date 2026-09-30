"""The teammate's guides say what the code does (task 7.23).

A guide that names a key, a port, a profile or a flag that the code has dropped sends the teammate
on a dead end on a machine nobody else can debug. These tests read the guides and compare them
with the code they describe: the .env block with the stack env's constants, the profile names with
the profile table, the ports with the compose file, the doctor's flags with its parser.
"""

from __future__ import annotations

import importlib.util
import re
import shlex

import pytest

from evaluation.mailguard_bench.guard_env import REPO_ROOT, V2_MAILGUARD_COMMIT
from evaluation.mailguard_bench.kit import doctor
from evaluation.mailguard_bench.live import stack_env
from evaluation.mailguard_bench.live.guard_worker import DEFAULT_PORT
from evaluation.mailguard_bench.model_profiles import PROFILES
from packages.core.settings import GEMINI_OPENAI_BASE_URL

GUIDE = (REPO_ROOT / "docs" / "BENCHMARK.md").read_text(encoding="utf-8")
NATIVE = (REPO_ROOT / "docs" / "benchmark-windows-native.md").read_text(encoding="utf-8")


def _fenced(text: str, language: str) -> list[str]:
    return re.findall(rf"```{language}\n(.*?)```", text, flags=re.DOTALL)


def test_the_env_block_is_the_runbooks_and_agrees_with_the_stack_env() -> None:
    [block] = [b for b in _fenced(GUIDE, "dotenv") if "EMBEDDING__MOCK" in b]
    values: dict[str, str] = {}
    for line in block.splitlines():
        name, _, rest = line.partition("=")
        if name and not name.startswith("#"):
            values[name] = rest.split("#")[0].strip()
    assert set(values) == {
        "BENCH_OPENAI_API_KEY",
        stack_env.EMBEDDING_KEY_ENV,
        *stack_env.HOST_MUST_SET,
    }
    assert values["EMBEDDING__MOCK"] == "false"
    assert values["EMBEDDING__MODEL_NAME"] == stack_env.EMBEDDING_MODEL
    assert values["EMBEDDING__DIMENSION"] == str(stack_env.EMBEDDING_DIMENSION)
    assert values["EMBEDDING__BASE_URL"] == GEMINI_OPENAI_BASE_URL
    assert values["RETRIEVAL__RETRIEVAL_TIMEOUT_MS"] == str(stack_env.RETRIEVAL_TIMEOUT_MS)
    assert float(values["LLM__TIMEOUT_S"]) == stack_env.DEFAULT_LLM_TIMEOUT_S


def test_the_two_old_lines_the_guide_names_are_the_ones_the_doctor_refuses() -> None:
    for name in doctor.OLD_ENV_LINES:
        assert name in GUIDE


def test_the_guides_use_only_profiles_that_exist() -> None:
    used = set(re.findall(r"(?:MODEL=|--model-profile )([a-z0-9.\-]+)", GUIDE + NATIVE))
    assert used, "the guides name no model profile"
    assert used <= set(PROFILES), used - set(PROFILES)


def test_the_models_are_run_in_the_order_the_runbook_fixes() -> None:
    positions = [
        GUIDE.index(name) for name in ("`gpt-4o-mini`", "`qwen2.5-7b`", "`llama-3.1-8b-local`")
    ]
    assert positions == sorted(positions)


def test_the_ports_the_guide_lists_are_the_ports_the_doctor_checks() -> None:
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    published = set(doctor.published_ports(compose, {})) | {DEFAULT_PORT}
    [row] = [line for line in GUIDE.splitlines() if "already allocated" in line]
    listed = {int(port) for port in re.findall(r"\b(\d{4,5})\b", row)}
    assert listed == published, listed ^ published


def test_the_doctor_flags_the_guides_use_exist() -> None:
    import argparse

    commands = re.findall(r"kit\.doctor ((?:--[a-z-]+ \S+ ?)+)", GUIDE + NATIVE)
    assert commands
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-profile")
    parser.add_argument("--reader")
    for command in commands:
        args = shlex.split(command.replace("<your reader model>", "reader-x"))
        parser.parse_args(args)  # an unknown flag raises SystemExit


def test_the_native_guide_pins_the_v2_guard_commit() -> None:
    assert V2_MAILGUARD_COMMIT in NATIVE
    assert V2_MAILGUARD_COMMIT in GUIDE


def test_every_repository_path_the_guides_name_exists() -> None:
    paths = set(
        re.findall(
            r"`((?:docs|evaluation)/[A-Za-z0-9_./\-]+\.(?:md|json|jsonl|joblib|py)|"
            r"evaluation/mailguard_bench/pinned/SHA256SUMS)`",
            GUIDE + NATIVE,
        )
    )
    missing = {p for p in paths if not (REPO_ROOT / p).exists() and "<" not in p}
    # the run folder and result files are created by a run, and docs/BENCHMARK.md names itself
    missing = {p for p in missing if "results/" not in p and "kit-log" not in p}
    assert missing == set(), missing


@pytest.mark.skipif(
    importlib.util.find_spec("evaluation.mailguard_bench.kit.campaign") is None,
    reason="kit/campaign.py arrives with work package R6a; this runs once it is merged",
)
def test_the_campaign_commands_of_the_native_guide_parse() -> None:
    campaign = importlib.import_module("evaluation.mailguard_bench.kit.campaign")

    lines = re.findall(
        r"^uv @mg evaluation\.mailguard_bench\.kit\.campaign (.+)$", NATIVE, flags=re.MULTILINE
    )
    assert lines
    for line in lines:
        campaign.parse_args(shlex.split(line.replace("<your reader model>", "reader-x")))
