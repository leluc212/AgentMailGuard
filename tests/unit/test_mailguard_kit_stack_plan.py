"""The stack env as a function the kit calls in-process (task 7.23; ADR-0012 decision 9).

``stack_env.run`` renders, refuses, writes and prints; the kit needs the same steps without the
printing, and the compose command as a list it can run. No docker, no model call, no network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from dotenv import dotenv_values

from evaluation.mailguard_bench.live.stack_env import (
    APP_SERVICES,
    StackEnvError,
    compose_command,
    prepare_stack,
)

EMBED_KEY = "embed-key-000"  # the embedding endpoint's key (here Gemini's, one worked example)
LLM_KEY = "llm-key-000"  # an LLM__OPENAI_API_KEY kept in .env; no profile of these tests uses it
OPENAI_KEY = "sk-openai-000"
HOST_ENV = {
    "BENCH_OPENAI_API_KEY": OPENAI_KEY,
    "LLM__OPENAI_API_KEY": LLM_KEY,
    "EMBEDDING__MOCK": "false",
    "EMBEDDING__MODEL_NAME": "gemini-embedding-001",
    "EMBEDDING__DIMENSION": "1536",
    "EMBEDDING__BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai",
    "EMBEDDING__API_KEY": EMBED_KEY,
    "RETRIEVAL__RETRIEVAL_TIMEOUT_MS": "3000",
    "RETRIEVAL__CATEGORY_FILTER_ENABLED": "false",
    "LLM__TIMEOUT_S": "60",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / ".gitignore").write_text(".env.stack\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "".join(f"{k}={v}\n" for k, v in HOST_ENV.items()), encoding="utf-8"
    )
    return tmp_path


def test_prepare_stack_writes_the_file_and_returns_the_command_as_a_list(repo: Path) -> None:
    plan = prepare_stack(
        "gpt-4o-mini", env_file=repo / ".env", out=repo / ".env.stack", process_env={}
    )
    assert plan.command == compose_command([repo / ".env", repo / ".env.stack"])
    assert plan.command[:2] == ["docker", "compose"]
    assert plan.command[-len(APP_SERVICES) :] == list(APP_SERVICES)
    written = dotenv_values(repo / ".env.stack", interpolate=False)
    assert written["LLM__OPENAI_API_KEY"] == OPENAI_KEY
    assert plan.profile.name == "gpt-4o-mini"
    assert plan.values["BENCH_SUMMARIZER_MODEL"] == "gpt-4o-mini"


def test_prepare_stack_names_only_the_stack_file_when_there_is_no_dot_env(repo: Path) -> None:
    (repo / ".env").unlink()
    process_env = {**HOST_ENV, "LLM__TIMEOUT_S": "60.0"}  # the shell holds what .env would
    plan = prepare_stack(
        "gemma-4-26b", env_file=repo / ".env", out=repo / ".env.stack", process_env=process_env
    )
    assert plan.env_files == [repo / ".env.stack"]


def test_prepare_stack_keeps_every_refusal_and_writes_nothing(repo: Path) -> None:
    (repo / ".env").write_text(
        "".join(
            f"{k}={v}\n" for k, v in {**HOST_ENV, "RETRIEVAL__RETRIEVAL_TIMEOUT_MS": "500"}.items()
        ),
        encoding="utf-8",
    )
    with pytest.raises(StackEnvError, match="RETRIEVAL__RETRIEVAL_TIMEOUT_MS must be 3000"):
        prepare_stack(
            "gpt-4o-mini", env_file=repo / ".env", out=repo / ".env.stack", process_env={}
        )
    assert not (repo / ".env.stack").exists()


def test_prepare_stack_refuses_a_shell_value_that_would_win_over_the_file(repo: Path) -> None:
    with pytest.raises(StackEnvError, match="LLM__OPENAI_API_KEY"):
        prepare_stack(
            "gpt-4o-mini",
            env_file=repo / ".env",
            out=repo / ".env.stack",
            process_env={"LLM__OPENAI_API_KEY": LLM_KEY},
        )
    assert not (repo / ".env.stack").exists()


def test_the_stack_file_is_written_where_os_fchmod_does_not_exist(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Python 3.12 on Windows has no os.fchmod; the kit must run there (ADR-0012 decision 9).
    monkeypatch.delattr("os.fchmod")
    plan = prepare_stack(
        "gpt-4o-mini", env_file=repo / ".env", out=repo / ".env.stack", process_env={}
    )
    assert plan.out.read_text(encoding="utf-8").startswith("# Container settings")
