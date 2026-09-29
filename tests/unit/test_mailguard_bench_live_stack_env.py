"""Unit tests for the live v2 benchmark's container environment (task 7.20; ADR-0011).

No docker, no model call, no network: the stack env is rendered from a temporary .env, and the
compose file, .env.example and the configuration reference are read as text (R24.5).
"""

from __future__ import annotations

import io
import json
import re
import shlex
import stat
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from dotenv import dotenv_values

from evaluation.mailguard_bench.live.stack_env import (
    APP_SERVICES,
    StackEnvError,
    compose_command,
    container_url,
    format_env_file,
    main,
    render_stack_env,
    shell_conflicts,
    write_env_file,
)
from evaluation.mailguard_bench.model_profiles import PROFILES, ModelProfileError, get_profile

GEMINI_KEY = "gemini-key-000"
OPENAI_KEY = "sk-openai-000"
# What `with_dot_env` yields for a .env holding both keys (demo-runbook §9.8 step 3).
DOT_ENV = {"BENCH_OPENAI_API_KEY": OPENAI_KEY, "LLM__OPENAI_API_KEY": GEMINI_KEY}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:11434/v1", "http://host.docker.internal:11434/v1"),
        ("http://127.0.0.1:11434/v1", "http://host.docker.internal:11434/v1"),
        ("http://[::1]:11434/v1", "http://host.docker.internal:11434/v1"),
        ("http://LOCALHOST/v1/", "http://host.docker.internal/v1/"),
        ("http://172.17.0.1:11434/v1", "http://172.17.0.1:11434/v1"),
        ("http://ollama-box.lan:11434/v1", "http://ollama-box.lan:11434/v1"),
        ("https://api.openai.com/v1", "https://api.openai.com/v1"),
    ],
)
def test_only_a_loopback_endpoint_is_pointed_at_the_host_from_a_container(
    url: str, expected: str
) -> None:
    # Inside a container `localhost` is the container itself, not the desktop's Ollama.
    assert container_url(url) == expected


def test_a_hosted_profile_renders_its_own_endpoint_key_and_model_for_every_tier() -> None:
    values = render_stack_env(get_profile("gpt-4o-mini"), DOT_ENV)
    assert values["LLM__PROVIDER"] == "openai"
    assert values["LLM__OPENAI_BASE_URL"] == "https://api.openai.com/v1"
    assert values["LLM__OPENAI_API_KEY"] == OPENAI_KEY  # the profile's key, not .env's Gemini key
    for tier in ("LLM__FAST_MODEL", "LLM__STRONG_MODEL", "LLM__FALLBACK_MODEL"):
        assert values[tier] == "gpt-4o-mini"
    assert json.loads(values["LLM__PRICE_TABLE"])["gpt-4o-mini"] == {
        "input_per_m": 0.15,
        "output_per_m": 0.6,
    }


def test_a_local_profile_reaches_ollama_on_the_host_through_the_container_alias() -> None:
    values = render_stack_env(get_profile("qwen2.5-7b"), DOT_ENV)
    assert values["LLM__OPENAI_BASE_URL"] == "http://host.docker.internal:11434/v1"
    assert values["LLM__OPENAI_API_KEY"] == "ollama"
    assert values["LLM__FAST_MODEL"] == "qwen2.5:7b-instruct"


def test_an_ollama_url_set_to_the_bridge_address_reaches_the_container_unchanged() -> None:
    environ = {**DOT_ENV, "BENCH_OLLAMA_BASE_URL": "http://172.17.0.1:11434/v1/"}
    values = render_stack_env(get_profile("llama-3.1-8b-local"), environ)
    assert values["LLM__OPENAI_BASE_URL"] == "http://172.17.0.1:11434/v1"


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_the_summarizer_uses_the_benchmarked_model_for_every_profile(name: str) -> None:
    # One benchmarked model in every LLM role, the summarizer included (owner decision).
    profile = get_profile(name)
    values = render_stack_env(profile, DOT_ENV)
    assert values["SUMMARIZATION__SUMMARIZER_MODEL"] == profile.model
    assert values["LLM__FAST_MODEL"] == profile.model


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_the_embedding_is_gemini_at_1536_with_the_key_from_dot_env_for_every_model(
    name: str,
) -> None:
    values = render_stack_env(get_profile(name), DOT_ENV)
    assert values["EMBEDDING__MOCK"] == "false"
    assert values["EMBEDDING__MODEL_NAME"] == "gemini-embedding-001"
    assert values["EMBEDDING__DIMENSION"] == "1536"
    assert values["EMBEDDING__BASE_URL"] == (
        "https://generativelanguage.googleapis.com/v1beta/openai"
    )
    # The Gemini key from .env, also when the model under test is OpenAI's: sending the OpenAI
    # key to Google (or the reverse) would fail every embedding call of the run.
    assert values["EMBEDDING__API_KEY"] == GEMINI_KEY


def test_retrieval_gets_a_budget_for_a_hosted_embedding_call_and_the_reranker_is_on() -> None:
    values = render_stack_env(get_profile("qwen2.5-7b"), DOT_ENV)
    assert values["RETRIEVAL__RETRIEVAL_TIMEOUT_MS"] == "3000"
    assert values["RETRIEVAL__RERANK_ENABLED"] == "true"
    assert values["RETRIEVAL__RERANK_MODEL"] == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert values["RETRIEVAL__RERANK_TIMEOUT_MS"] == "1000"
    # The runtime image carries the model and sets its own path; a value here would override it.
    assert "RETRIEVAL__RERANK_MODEL_DIR" not in values


def test_the_llm_timeout_is_the_benchmark_runs_60_seconds_and_can_be_changed() -> None:
    profile = get_profile("qwen2.5-7b")
    assert render_stack_env(profile, DOT_ENV)["LLM__TIMEOUT_S"] == "60.0"
    assert render_stack_env(profile, DOT_ENV, llm_timeout_s=120)["LLM__TIMEOUT_S"] == "120.0"
    with pytest.raises(StackEnvError, match="LLM__TIMEOUT_S"):
        render_stack_env(profile, DOT_ENV, llm_timeout_s=0)


@pytest.mark.parametrize("environ", [{}, {"LLM__OPENAI_API_KEY": "  "}])
def test_a_missing_gemini_key_fails_and_names_the_variable(environ: dict[str, str]) -> None:
    # Ollama needs no LLM key, but every run embeds with Gemini, whose key lives in .env.
    with pytest.raises(StackEnvError, match="LLM__OPENAI_API_KEY"):
        render_stack_env(get_profile("qwen2.5-7b"), environ)


def test_a_missing_profile_key_fails_as_the_profile_does() -> None:
    with pytest.raises(ModelProfileError, match="BENCH_OPENAI_API_KEY"):
        render_stack_env(get_profile("gpt-4o-mini"), {"LLM__OPENAI_API_KEY": GEMINI_KEY})


def test_the_env_file_quotes_every_value_literally_and_reads_back_unchanged() -> None:
    values = render_stack_env(get_profile("gpt-4o-mini"), DOT_ENV)
    text = format_env_file(values, header="model gpt-4o-mini\nholds API keys")
    body = [line for line in text.splitlines() if not line.startswith("#")]
    assert body and all(re.fullmatch(r"[A-Z0-9_]+='[^']*'", line) for line in body)
    assert text.startswith("# model gpt-4o-mini\n# holds API keys\n")
    # The price table is JSON: braces, colons and double quotes must survive.
    assert dotenv_values(stream=io.StringIO(text), interpolate=False) == values


def test_a_dollar_sign_is_written_literally_so_compose_does_not_interpolate_it() -> None:
    # Compose interpolates unquoted and double-quoted env-file values; single quotes are literal.
    assert format_env_file({"KEY": "a$b${C}"}) == "KEY='a$b${C}'\n"


@pytest.mark.parametrize("bad", ["it's", "two\nlines", "back\\slash", "cr\rvalue"])
def test_a_value_the_env_file_cannot_hold_literally_is_refused_without_echoing_it(
    bad: str,
) -> None:
    with pytest.raises(StackEnvError, match="KEY") as raised:
        format_env_file({"KEY": bad})
    assert bad not in str(raised.value)  # the value may be a key


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git work tree whose .gitignore covers .env.stack and out/."""
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(".env.stack\nout/\n", encoding="utf-8")
    return tmp_path


def test_the_file_is_written_owner_only_into_a_path_git_ignores(repo: Path) -> None:
    out = repo / ".env.stack"
    write_env_file(out, {"LLM__OPENAI_API_KEY": "k-1"}, header="h")
    assert out.read_text(encoding="utf-8") == "# h\nLLM__OPENAI_API_KEY='k-1'\n"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_an_existing_world_readable_file_is_made_owner_only(repo: Path) -> None:
    out = repo / ".env.stack"
    out.write_text("OLD='x'\n", encoding="utf-8")
    out.chmod(0o644)
    write_env_file(out, {"NEW": "y"})
    assert out.read_text(encoding="utf-8") == "NEW='y'\n"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_a_missing_directory_is_created_when_git_ignores_the_path(repo: Path) -> None:
    out = repo / "out" / "deep" / "stack.env"
    write_env_file(out, {"K": "v"})
    assert out.read_text(encoding="utf-8") == "K='v'\n"


def test_a_path_git_does_not_ignore_is_refused_and_nothing_is_written(repo: Path) -> None:
    out = repo / "stack.env"
    with pytest.raises(StackEnvError, match="not git-ignored"):
        write_env_file(out, {"K": "v"})
    assert not out.exists()


def test_a_tracked_file_is_refused_even_when_a_pattern_matches_it(repo: Path) -> None:
    out = repo / ".env.stack"
    out.write_text("OLD='x'\n", encoding="utf-8")
    _git(repo, "add", "--force", ".env.stack")  # tracked: git no longer treats it as ignored
    with pytest.raises(StackEnvError, match="not git-ignored"):
        write_env_file(out, {"K": "v"})
    assert out.read_text(encoding="utf-8") == "OLD='x'\n"


def test_a_path_outside_any_work_tree_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))  # no repo above tmp_path
    out = tmp_path / "sub" / "stack.env"
    write_env_file(out, {"K": "v"})
    assert out.read_text(encoding="utf-8") == "K='v'\n"


def test_a_value_the_file_cannot_hold_is_refused_before_anything_is_created(repo: Path) -> None:
    out = repo / "out" / "stack.env"
    with pytest.raises(StackEnvError, match="KEY"):
        write_env_file(out, {"KEY": "it's"})
    assert not out.parent.exists()


def test_without_git_the_check_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_git(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)
    out = tmp_path / "stack.env"
    with pytest.raises(StackEnvError, match="git"):
        write_env_file(out, {"K": "v"})
    assert not out.exists()


def test_the_command_recreates_only_the_model_services_with_no_dependencies() -> None:
    command = compose_command([Path(".env"), Path(".env.stack")])
    assert shlex.join(command) == (
        "docker compose --env-file .env --env-file .env.stack up -d --no-deps "
        "api triage-worker knowledge-worker ai-worker"
    )
    # Postgres, RabbitMQ and MinIO hold the run's data and are never restarted by it.
    assert not {"postgres", "rabbitmq", "minio", "init"} & set(command)


def test_the_command_names_only_the_stack_file_when_there_is_no_dot_env() -> None:
    command = compose_command([Path(".env.stack")])
    assert shlex.join(command).startswith("docker compose --env-file .env.stack up -d --no-deps")
    assert tuple(command[-len(APP_SERVICES) :]) == APP_SERVICES


def test_a_shell_value_that_differs_is_a_conflict_because_compose_prefers_the_shell() -> None:
    values = {"LLM__FAST_MODEL": "m", "EMBEDDING__MOCK": "false", "LLM__PROVIDER": "openai"}
    shell = {"LLM__FAST_MODEL": "other", "EMBEDDING__MOCK": "false", "UNRELATED": "x"}
    assert shell_conflicts(values, shell) == ["LLM__FAST_MODEL"]


# --- the command line -----------------------------------------------------------------------

DOT_ENV_TEXT = f"BENCH_OPENAI_API_KEY={OPENAI_KEY}\nLLM__OPENAI_API_KEY={GEMINI_KEY}\n"
COMMAND = (
    "docker compose --env-file .env --env-file .env.stack up -d --no-deps "
    "api triage-worker knowledge-worker ai-worker"
)


@pytest.fixture
def cli_repo(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo with a .env holding both keys, as the cwd, and none of the settings exported."""
    (repo / ".env").write_text(DOT_ENV_TEXT, encoding="utf-8")
    monkeypatch.chdir(repo)
    # tests/conftest.py exports LLM__PROVIDER and EMBEDDING__MOCK for every unit test.
    for name in [*render_stack_env(get_profile("gpt-4o-mini"), DOT_ENV), *DOT_ENV]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("BENCH_OLLAMA_BASE_URL", raising=False)
    return repo


def test_main_writes_the_file_prints_the_command_and_never_a_key(
    cli_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--model-profile", "gpt-4o-mini"]) == 0
    captured = capsys.readouterr()
    assert COMMAND in captured.out.splitlines()
    for secret in (OPENAI_KEY, GEMINI_KEY):
        assert secret not in captured.out + captured.err
    written = dotenv_values(cli_repo / ".env.stack", interpolate=False)
    assert written["LLM__OPENAI_API_KEY"] == OPENAI_KEY
    assert written["EMBEDDING__API_KEY"] == GEMINI_KEY
    assert written["SUMMARIZATION__SUMMARIZER_MODEL"] == "gpt-4o-mini"
    assert stat.S_IMODE((cli_repo / ".env.stack").stat().st_mode) == 0o600


def test_main_reads_keys_from_dot_env_but_the_environment_wins(
    cli_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BENCH_OPENAI_API_KEY", "sk-from-the-environment")
    assert main(["--model-profile", "gpt-4o-mini"]) == 0
    written = dotenv_values(cli_repo / ".env.stack", interpolate=False)
    assert written["LLM__OPENAI_API_KEY"] == "sk-from-the-environment"
    assert written["EMBEDDING__API_KEY"] == GEMINI_KEY  # only the shell's own value moved


def test_main_names_only_the_stack_file_when_there_is_no_dot_env(
    cli_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (cli_repo / ".env").unlink()
    # The Gemma profile's LLM key is the Gemini key itself, so exporting it conflicts with nothing.
    monkeypatch.setenv("LLM__OPENAI_API_KEY", GEMINI_KEY)
    assert main(["--model-profile", "gemma-4-26b"]) == 0
    assert "docker compose --env-file .env.stack up -d --no-deps" in capsys.readouterr().out


def test_an_exported_gemini_key_is_refused_for_another_profile_because_the_shell_would_win(
    cli_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Compose would hand the containers the shell's Gemini key as their LLM key, so a GPT-4o-mini
    # run would send it to api.openai.com. The message names the setting, not the key.
    monkeypatch.setenv("LLM__OPENAI_API_KEY", GEMINI_KEY)
    assert main(["--model-profile", "gpt-4o-mini"]) == 1
    err = capsys.readouterr().err
    assert "LLM__OPENAI_API_KEY" in err and GEMINI_KEY not in err
    assert not (cli_repo / ".env.stack").exists()


def test_main_can_change_the_llm_timeout_and_the_output_path(cli_repo: Path) -> None:
    assert (
        main(["--model-profile", "qwen2.5-7b", "--llm-timeout-s", "90", "--out", "out/s.env"]) == 0
    )
    written = dotenv_values(cli_repo / "out" / "s.env", interpolate=False)
    assert written["LLM__TIMEOUT_S"] == "90.0"


@pytest.mark.parametrize(
    ("dot_env", "profile", "variable"),
    [
        ("LLM__OPENAI_API_KEY=g\n", "gpt-4o-mini", "BENCH_OPENAI_API_KEY"),
        ("BENCH_OPENAI_API_KEY=o\n", "gpt-4o-mini", "LLM__OPENAI_API_KEY"),
    ],
)
def test_main_fails_naming_the_missing_key_and_writes_nothing(
    cli_repo: Path,
    capsys: pytest.CaptureFixture[str],
    dot_env: str,
    profile: str,
    variable: str,
) -> None:
    (cli_repo / ".env").write_text(dot_env, encoding="utf-8")
    assert main(["--model-profile", profile]) == 1
    err = capsys.readouterr().err
    assert err.startswith("FAIL ") and variable in err
    assert not (cli_repo / ".env.stack").exists()


def test_main_refuses_when_the_shell_sets_another_value_and_writes_nothing(
    cli_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LLM__FAST_MODEL", "some-other-model")
    monkeypatch.setenv("EMBEDDING__MOCK", "true")
    assert main(["--model-profile", "gpt-4o-mini"]) == 1
    err = capsys.readouterr().err
    assert "LLM__FAST_MODEL" in err and "EMBEDDING__MOCK" in err
    assert "some-other-model" not in err  # names only: a shell value can be a key
    assert not (cli_repo / ".env.stack").exists()


def test_a_shell_value_equal_to_the_rendered_one_is_no_conflict(
    cli_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM__FAST_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("EMBEDDING__MOCK", "false")
    assert main(["--model-profile", "gpt-4o-mini"]) == 0


def test_main_refuses_a_path_git_does_not_ignore(
    cli_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--model-profile", "qwen2.5-7b", "--out", "stack.env"]) == 1
    assert "not git-ignored" in capsys.readouterr().err
    assert not (cli_repo / "stack.env").exists()


def test_main_never_runs_docker(cli_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executables: list[str] = []
    real_run = subprocess.run

    def spy(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        executables.append(command[0])
        return cast(subprocess.CompletedProcess[str], real_run(command, **kwargs))

    monkeypatch.setattr(subprocess, "run", spy)
    assert main(["--model-profile", "qwen2.5-7b"]) == 0
    assert executables and set(executables) == {"git"}


def test_the_parser_refuses_an_unknown_profile() -> None:
    with pytest.raises(SystemExit):
        main(["--model-profile", "gpt-5"])
