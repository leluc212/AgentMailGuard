"""Unit tests for the live v2 benchmark's container environment (task 7.20; ADR-0011).

No docker, no model call, no network: the stack env is rendered from a temporary .env, and the
compose file, .env.example and the configuration reference are read as text (R24.5).
"""

from __future__ import annotations

import fnmatch
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from dotenv import dotenv_values

from evaluation.mailguard_bench.live.stack_env import (
    APP_SERVICES,
    DEFAULT_OUT,
    StackEnvError,
    compose_command,
    container_url,
    format_env_file,
    host_env_problems,
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
# Plus what §9.9 step 1 has the owner keep there for the host processes. The guard-worker and the
# runner read .env and the shell, never .env.stack, so these must say what the containers get.
HOST_ENV = {
    **DOT_ENV,
    "EMBEDDING__MOCK": "false",
    "EMBEDDING__MODEL_NAME": "gemini-embedding-001",
    "EMBEDDING__DIMENSION": "1536",
    "EMBEDDING__BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai",
    "EMBEDDING__API_KEY": GEMINI_KEY,
    "RETRIEVAL__RETRIEVAL_TIMEOUT_MS": "3000",
    "RETRIEVAL__CATEGORY_FILTER_ENABLED": "false",
    "LLM__TIMEOUT_S": "60",
}


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
    # One benchmarked model in every LLM role, the summarizer included (owner decision). It
    # travels under a stack-only name that docker-compose.yml maps to the containers'
    # SUMMARIZATION__SUMMARIZER_MODEL, so a line of that name in .env never reaches a container.
    profile = get_profile(name)
    values = render_stack_env(profile, DOT_ENV)
    assert values["BENCH_SUMMARIZER_MODEL"] == profile.model
    assert "SUMMARIZATION__SUMMARIZER_MODEL" not in values
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


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_the_category_filter_is_off_for_every_model(name: str) -> None:
    # Live triage picks the category; the case KB is filed under the case's own (ADR-0013).
    assert render_stack_env(get_profile(name), DOT_ENV)["RETRIEVAL__CATEGORY_FILTER_ENABLED"] == (
        "false"
    )


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_the_category_retrieval_floor_is_pinned_on_for_every_model(name: str) -> None:
    # The floor is what makes the RAG path run (ADR-0013). Compose forwards it from .env, and a
    # leftover `false` there would silently disable it, so the stack env (which Compose reads
    # after .env) states it like every other setting the run depends on.
    values = render_stack_env(get_profile(name), DOT_ENV)
    assert values["TRIAGE__CATEGORY_RETRIEVAL_FLOOR"] == "true"


def test_a_dot_env_that_switches_the_floor_off_does_not_reach_the_containers() -> None:
    # Compose: a later --env-file wins, and compose_command puts .env.stack after .env.
    command = compose_command([Path("/repo/.env"), Path("/repo/.env.stack")])
    assert command.index("/repo/.env.stack") > command.index("/repo/.env")
    values = render_stack_env(
        get_profile("gpt-4o-mini"), {**DOT_ENV, "TRIAGE__CATEGORY_RETRIEVAL_FLOOR": "false"}
    )
    assert values["TRIAGE__CATEGORY_RETRIEVAL_FLOOR"] == "true"


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


# --- the host processes read what the containers read -----------------------------------------
# C0 drafts in the ai-worker container (settings from compose, .env.stack and defaults). C0T to C3
# draft in the guard-worker, a host process that reads .env and the shell, never .env.stack; the
# runner builds its fingerprint and its lane list from the same host environment. A .env that
# disagrees would run the guarded configs on other settings than C0, with no error to show it.


def _problems(profile_name: str, environ: dict[str, str], **render: Any) -> list[str]:
    profile = get_profile(profile_name)
    return host_env_problems(render_stack_env(profile, environ, **render), environ)


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_a_dot_env_that_says_what_the_containers_get_has_no_disagreement(name: str) -> None:
    assert _problems(name, HOST_ENV) == []


@pytest.mark.parametrize(
    ("setting", "stale"),
    [
        # The .env.example of before task 7.20 set a 500 ms budget and a 15 s timeout.
        ("RETRIEVAL__RETRIEVAL_TIMEOUT_MS", "500"),
        ("LLM__TIMEOUT_S", "15.0"),
        ("EMBEDDING__MOCK", "true"),
        ("EMBEDDING__MODEL_NAME", "text-embedding-3-small"),
        ("EMBEDDING__DIMENSION", "768"),
        ("EMBEDDING__BASE_URL", "https://api.openai.com/v1"),
        ("EMBEDDING__API_KEY", "not-the-gemini-key"),
        ("RETRIEVAL__RERANK_ENABLED", "false"),
        ("RETRIEVAL__RERANK_MODEL", "other/cross-encoder"),
        ("RETRIEVAL__RERANK_TIMEOUT_MS", "50"),
        # The production value: C0 would search without the category filter, the guarded
        # configs with it.
        ("RETRIEVAL__CATEGORY_FILTER_ENABLED", "true"),
        # Settings no container reads from a file: an older .env still carries them.
        ("SUMMARIZATION__SUMMARIZER_MODEL", "gpt-4o-mini"),
        ("ROUTING__CONFIGURED_CONSUMERS", '["email.support.*"]'),
    ],
)
def test_a_dot_env_that_disagrees_with_the_containers_is_named(setting: str, stale: str) -> None:
    problems = _problems("qwen2.5-7b", {**HOST_ENV, setting: stale})
    assert len(problems) == 1 and problems[0].startswith(setting), problems


@pytest.mark.parametrize(
    "setting",
    [
        "EMBEDDING__MOCK",
        "EMBEDDING__MODEL_NAME",
        "EMBEDDING__DIMENSION",
        "EMBEDDING__BASE_URL",
        "EMBEDDING__API_KEY",
        "LLM__TIMEOUT_S",
        "RETRIEVAL__RETRIEVAL_TIMEOUT_MS",
        "RETRIEVAL__CATEGORY_FILTER_ENABLED",
    ],
)
def test_a_dot_env_that_leaves_out_a_setting_the_host_must_state_is_named(setting: str) -> None:
    # Unset on the host means the code default (a mock embedder, 15 s, the category filter on),
    # not the container's value; the vector column's width is stated too rather than left to a
    # default (R5.10).
    problems = _problems("qwen2.5-7b", {k: v for k, v in HOST_ENV.items() if k != setting})
    assert len(problems) == 1 and problems[0].startswith(setting), problems


@pytest.mark.parametrize(
    "change",
    [
        {"SUMMARIZATION__SUMMARIZER_MODEL": ""},  # blank means unset
        {"SUMMARIZATION__SUMMARIZER_MODEL": " qwen2.5:7b-instruct "},  # the containers' model
        {"ROUTING__CONFIGURED_CONSUMERS": ""},
        {"LLM__TIMEOUT_S": "60.0"},
        {"LLM__TIMEOUT_S": "060"},
        {"EMBEDDING__MOCK": "False"},
        {"EMBEDDING__MOCK": "0"},
        {"EMBEDDING__BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai/"},
        {"RETRIEVAL__RETRIEVAL_TIMEOUT_MS": "03000"},
        {"RETRIEVAL__CATEGORY_FILTER_ENABLED": "False"},
        {"RETRIEVAL__CATEGORY_FILTER_ENABLED": "0"},
        {"RETRIEVAL__CATEGORY_FILTER_ENABLED": "off"},
        {
            "RETRIEVAL__RERANK_ENABLED": "TRUE",
            "RETRIEVAL__RERANK_MODEL": "cross-encoder/ms-marco-MiniLM-L-6-v2",
            "RETRIEVAL__RERANK_TIMEOUT_MS": "1000",
        },
    ],
)
def test_another_spelling_of_what_the_containers_get_is_no_disagreement(
    change: dict[str, str],
) -> None:
    assert _problems("qwen2.5-7b", {**HOST_ENV, **change}) == []


def test_the_summarizer_may_only_be_the_profiles_own_model_on_the_host() -> None:
    # Another profile's model is not the containers' model, even if it is a benchmarked one.
    problems = _problems(
        "qwen2.5-7b", {**HOST_ENV, "SUMMARIZATION__SUMMARIZER_MODEL": "llama3.1:8b"}
    )
    assert len(problems) == 1 and problems[0].startswith("SUMMARIZATION__SUMMARIZER_MODEL")
    assert "qwen2.5:7b-instruct" in problems[0]  # what to set instead


def test_a_custom_llm_timeout_must_be_in_dot_env_too() -> None:
    profile = get_profile("qwen2.5-7b")
    values = render_stack_env(profile, HOST_ENV, llm_timeout_s=90)  # .env says 60
    problems = host_env_problems(values, HOST_ENV)
    assert len(problems) == 1 and problems[0].startswith("LLM__TIMEOUT_S")
    assert "90" in problems[0]


def test_every_disagreement_is_listed_at_once() -> None:
    old_example = {
        **DOT_ENV,
        "EMBEDDING__MOCK": "true",
        "EMBEDDING__DIMENSION": "1536",
        "RETRIEVAL__RETRIEVAL_TIMEOUT_MS": "500",
        "RETRIEVAL__CATEGORY_FILTER_ENABLED": "true",
        "LLM__TIMEOUT_S": "15.0",
        "SUMMARIZATION__SUMMARIZER_MODEL": "gpt-4o-mini",
        "ROUTING__CONFIGURED_CONSUMERS": '["email.support.*"]',
    }
    named = {problem.split(" ")[0] for problem in _problems("qwen2.5-7b", old_example)}
    assert {
        "EMBEDDING__MOCK",
        "EMBEDDING__MODEL_NAME",
        "EMBEDDING__BASE_URL",
        "EMBEDDING__API_KEY",
        "RETRIEVAL__RETRIEVAL_TIMEOUT_MS",
        "RETRIEVAL__CATEGORY_FILTER_ENABLED",
        "LLM__TIMEOUT_S",
        "SUMMARIZATION__SUMMARIZER_MODEL",
        "ROUTING__CONFIGURED_CONSUMERS",
    } <= named
    assert "EMBEDDING__DIMENSION" not in named  # 1536 was right


def test_a_disagreement_names_settings_and_never_echoes_a_value_or_a_key() -> None:
    environ = {
        **HOST_ENV,
        "EMBEDDING__API_KEY": "host-side-key-123",
        "SUMMARIZATION__SUMMARIZER_MODEL": "host-only-model",
    }
    text = " ".join(_problems("qwen2.5-7b", environ))
    for secret in ("host-side-key-123", "host-only-model", GEMINI_KEY, OPENAI_KEY):
        assert secret not in text


# --- the command line -----------------------------------------------------------------------

DOT_ENV_TEXT = "".join(f"{name}={value}\n" for name, value in HOST_ENV.items())
COMMAND = (
    "docker compose --env-file .env --env-file .env.stack up -d --no-deps "
    "api triage-worker knowledge-worker ai-worker"
)
# Read by the host processes and by nothing in the stack env; an old .env may carry them.
HOST_ONLY = ("SUMMARIZATION__SUMMARIZER_MODEL", "ROUTING__CONFIGURED_CONSUMERS")


def _dot_env(**changes: str | None) -> str:
    """HOST_ENV with some lines changed or (None) removed, as the text of a .env."""
    merged = {**HOST_ENV, **changes}
    return "".join(f"{name}={value}\n" for name, value in merged.items() if value is not None)


@pytest.fixture
def cli_repo(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo with the step 1 .env, as the cwd, and none of the settings exported."""
    (repo / ".env").write_text(DOT_ENV_TEXT, encoding="utf-8")
    monkeypatch.chdir(repo)
    # tests/conftest.py exports LLM__PROVIDER and EMBEDDING__MOCK for every unit test.
    for name in [*render_stack_env(get_profile("gpt-4o-mini"), DOT_ENV), *HOST_ENV, *HOST_ONLY]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("BENCH_OLLAMA_BASE_URL", raising=False)
    return repo


def test_main_writes_the_file_prints_the_command_and_never_a_key(
    cli_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--model-profile", "gpt-4o-mini"]) == 0
    captured = capsys.readouterr()
    assert COMMAND in captured.out.splitlines()
    assert any(line.strip().startswith("host") for line in captured.out.splitlines())
    for secret in (OPENAI_KEY, GEMINI_KEY):
        assert secret not in captured.out + captured.err
    written = dotenv_values(cli_repo / ".env.stack", interpolate=False)
    assert written["LLM__OPENAI_API_KEY"] == OPENAI_KEY
    assert written["EMBEDDING__API_KEY"] == GEMINI_KEY
    assert written["BENCH_SUMMARIZER_MODEL"] == "gpt-4o-mini"
    assert "SUMMARIZATION__SUMMARIZER_MODEL" not in written
    assert written["RETRIEVAL__CATEGORY_FILTER_ENABLED"] == "false"
    assert "category filter off" in captured.out  # the owner is told what the containers get
    assert written["TRIAGE__CATEGORY_RETRIEVAL_FLOOR"] == "true"
    assert "category retrieval floor on" in captured.out
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
    # Everything in the shell: the host processes read it from there, and the shell values equal
    # the rendered ones (a shell value is compared as text: "60.0", not "60"), so nothing
    # conflicts (the Gemma profile's LLM key is the Gemini key).
    for name, value in {**HOST_ENV, "LLM__TIMEOUT_S": "60.0"}.items():
        monkeypatch.setenv(name, value)
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
    (cli_repo / ".env").write_text(_dot_env(LLM__TIMEOUT_S="90"), encoding="utf-8")
    assert (
        main(["--model-profile", "qwen2.5-7b", "--llm-timeout-s", "90", "--out", "out/s.env"]) == 0
    )
    written = dotenv_values(cli_repo / "out" / "s.env", interpolate=False)
    assert written["LLM__TIMEOUT_S"] == "90.0"


def test_main_refuses_a_timeout_that_the_host_processes_would_not_share(
    cli_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # .env says 60: the guard-worker would time out at 60 s while the containers wait 90 s.
    assert main(["--model-profile", "qwen2.5-7b", "--llm-timeout-s", "90"]) == 1
    assert "LLM__TIMEOUT_S" in capsys.readouterr().err
    assert not (cli_repo / ".env.stack").exists()


def test_main_refuses_a_dot_env_from_before_task_7_20_and_writes_nothing(
    cli_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The old .env.example named a summarizer model nothing read (the guard-worker now honours
    # it: it would ask Ollama for gpt-4o-mini), a 500 ms retrieval budget and a lane list that
    # lacks email.administration.priority, which the containers do consume.
    (cli_repo / ".env").write_text(
        _dot_env(
            RETRIEVAL__RETRIEVAL_TIMEOUT_MS="500",
            SUMMARIZATION__SUMMARIZER_MODEL="gpt-4o-mini",
            ROUTING__CONFIGURED_CONSUMERS='["email.support.*"]',
        ),
        encoding="utf-8",
    )
    assert main(["--model-profile", "qwen2.5-7b"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("FAIL ")
    for name in ("RETRIEVAL__RETRIEVAL_TIMEOUT_MS", *HOST_ONLY):
        assert name in err
    assert "gpt-4o-mini" not in err and "email.support" not in err  # names, not values
    assert "docs/demo-runbook.md" in err  # where the fix is written down
    assert not (cli_repo / ".env.stack").exists()


@pytest.mark.parametrize("name", HOST_ONLY)
def test_an_exported_host_only_setting_is_refused_too(
    cli_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    # The host processes read the shell as well as .env; a container reads neither of these.
    monkeypatch.setenv(name, "gpt-4o-mini")
    assert main(["--model-profile", "qwen2.5-7b"]) == 1
    assert name in capsys.readouterr().err
    assert not (cli_repo / ".env.stack").exists()


def test_a_dot_env_without_the_host_settings_is_refused_naming_them(
    cli_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Only the keys: the host processes would embed with the mock embedder while the knowledge
    # worker container embedded the corpus with Gemini.
    (cli_repo / ".env").write_text(
        "".join(f"{name}={value}\n" for name, value in DOT_ENV.items()), encoding="utf-8"
    )
    assert main(["--model-profile", "qwen2.5-7b"]) == 1
    err = capsys.readouterr().err
    for name in (
        "EMBEDDING__MOCK",
        "EMBEDDING__MODEL_NAME",
        "LLM__TIMEOUT_S",
        "RETRIEVAL__CATEGORY_FILTER_ENABLED",
    ):
        assert name in err
    assert not (cli_repo / ".env.stack").exists()


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


def test_main_refuses_a_shell_that_switches_the_floor_off_and_writes_nothing(
    cli_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRIAGE__CATEGORY_RETRIEVAL_FLOOR", "false")
    assert main(["--model-profile", "gpt-4o-mini"]) == 1
    assert "TRIAGE__CATEGORY_RETRIEVAL_FLOOR" in capsys.readouterr().err
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


# --- the config surface the stack env depends on ---------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# The keys package F documents (specs/superpowers/specs/2026-09-29-mailguard-live-v2-design.md, F).
V2_KEYS = (
    "OBJECT_STORAGE__BUCKET_HTML",
    "RETRIEVAL__RETRIEVAL_TIMEOUT_MS",
    "RETRIEVAL__RERANK_MODEL",
    "RETRIEVAL__RERANK_MODEL_DIR",
    "RETRIEVAL__RERANK_TIMEOUT_MS",
    "RETRIEVAL__RERANK_ENABLED",
    "RETRIEVAL__CATEGORY_FILTER_ENABLED",
    "SUMMARIZATION__SUMMARIZER_MODEL",
    "EMBEDDING__MOCK",
    "EMBEDDING__MODEL_NAME",
    "EMBEDDING__BASE_URL",
)


def _compose() -> dict[str, Any]:
    loaded = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return cast(dict[str, Any], loaded)


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _git_ignores(name: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "--quiet", "--", name],
        capture_output=True,
        check=False,
    )
    if result.returncode == 128:  # not a git checkout (an exported tree): read the file instead
        return name in _read(".gitignore").splitlines()
    return result.returncode == 0


def test_the_default_stack_env_file_is_git_ignored() -> None:
    assert _git_ignores(DEFAULT_OUT)


def test_the_default_stack_env_file_never_enters_a_docker_build_context() -> None:
    lines = _read(".dockerignore").splitlines()
    patterns = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    assert any(fnmatch.fnmatch(DEFAULT_OUT, pattern) for pattern in patterns)


@pytest.mark.parametrize("service", APP_SERVICES)
def test_the_model_services_carry_every_setting_the_stack_env_renders(service: str) -> None:
    environment = _compose()["services"][service]["environment"]
    interpolated = " ".join(str(value) for value in environment.values() if value is not None)
    for name in render_stack_env(get_profile("gpt-4o-mini"), DOT_ENV):
        if name in environment:  # forwarded under its own name, from itself
            forwarded = environment[name]
            assert forwarded is None or str(forwarded).startswith("${" + name), (service, name)
        else:  # or read by another entry's interpolation (the summarizer's stack-only name)
            assert "${" + name in interpolated, f"{service} never reads {name}: it is cosmetic"


@pytest.mark.parametrize("service", APP_SERVICES)
def test_the_containers_get_the_summarizer_model_only_from_the_stack_only_name(
    service: str,
) -> None:
    # The .env.example of before task 7.20 set SUMMARIZATION__SUMMARIZER_MODEL=gpt-4o-mini and
    # the ai-worker now honours the setting. A container that read that line of an old .env
    # would ask a Gemini or an Ollama endpoint for gpt-4o-mini, and every thread over the
    # summarization threshold would fail its summary. A blank value means unset (settings).
    environment = _compose()["services"][service]["environment"]
    assert environment["SUMMARIZATION__SUMMARIZER_MODEL"] == "${BENCH_SUMMARIZER_MODEL:-}"


def test_no_compose_entry_reads_a_summarizer_line_from_dot_env() -> None:
    # Neither by interpolation nor as a value-less key (which Compose resolves from .env).
    assert "${SUMMARIZATION__SUMMARIZER_MODEL" not in _read("docker-compose.yml")
    for name, forwarded in _compose()["x-app-env"].items():
        assert forwarded is not None or name != "SUMMARIZATION__SUMMARIZER_MODEL"


def test_only_the_services_that_call_a_model_reach_the_host_by_name() -> None:
    services = _compose()["services"]
    holders = sorted(name for name, service in services.items() if "extra_hosts" in service)
    assert holders == sorted(APP_SERVICES)
    for name in holders:
        assert services[name]["extra_hosts"] == ["host.docker.internal:host-gateway"]


def test_the_command_names_application_services_only() -> None:
    services = _compose()["services"]
    assert set(APP_SERVICES) <= set(services)
    assert not set(APP_SERVICES) & {
        "postgres",
        "rabbitmq",
        "minio",
        "prometheus",
        "grafana",
        "init",
    }


def test_the_image_owns_the_reranker_model_dir_and_compose_never_forwards_it() -> None:
    # A value-less key that nothing resolves reaches Docker as a bare name, and a bare name unsets
    # the variable in the container, the image's ENV included. Seen live on 2026-09-30: the
    # ai-worker lost the image's RETRIEVAL__RERANK_MODEL_DIR and read the Hugging Face cache
    # instead of the model baked into the image. So the image alone sets it.
    environment = _compose()["x-app-env"]
    assert "RETRIEVAL__RERANK_MODEL_DIR" not in environment


def test_compose_forwards_the_category_filter_only_when_something_sets_it() -> None:
    # Value-less, like the other optional retrieval settings: unset (a bare name), the settings
    # default, true, applies. Right only because the image does not set it (see
    # test_compose_never_forwards_a_bare_name_the_image_sets in test_runtime_image_contract.py).
    environment = _compose()["x-app-env"]
    assert "RETRIEVAL__CATEGORY_FILTER_ENABLED" in environment
    assert environment["RETRIEVAL__CATEGORY_FILTER_ENABLED"] is None


def test_env_example_never_sets_an_optional_setting_to_blank() -> None:
    # Compose resolves a value-less key from .env, so `NAME=` there would be forwarded blank.
    values = dotenv_values(REPO_ROOT / ".env.example")
    optional = [name for name, forwarded in _compose()["x-app-env"].items() if forwarded is None]
    assert optional, "expected value-less (optional) settings in x-app-env"
    for name in optional:
        assert values.get(name) != "", name


def test_the_retrieval_budget_is_3000_ms_in_compose_env_example_and_the_reference() -> None:
    name = "RETRIEVAL__RETRIEVAL_TIMEOUT_MS"
    assert _compose()["x-app-env"][name] == "${" + name + ":-3000}"
    assert dotenv_values(REPO_ROOT / ".env.example")[name] == "3000"
    assert _default_cell(name) == "`3000`"


def test_a_fresh_copy_of_env_example_sets_nothing_the_host_check_refuses() -> None:
    # dotenv skips comment lines, so this reads the active lines only. The containers read
    # neither setting from a file, so a copy that set them would run the guard-worker (a host
    # process that reads .env) on other settings than the ai-worker container.
    active = dotenv_values(REPO_ROOT / ".env.example")
    for name in HOST_ONLY:
        assert name not in active, f"{name} must stay commented out in .env.example"
    assert active["RETRIEVAL__RETRIEVAL_TIMEOUT_MS"] == "3000"
    # A fresh copy keeps production's category filter; only the benchmark's .env (runbook 9.9
    # step 1) turns it off, and the host check refuses a benchmark run while it is on.
    assert active["RETRIEVAL__CATEGORY_FILTER_ENABLED"] == "true"


def _default_cell(name: str) -> str:
    row = re.search(rf"^\| `{name}` \| [^|]+ \| ([^|]+) \|", _read("docs/configuration.md"), re.M)
    assert row, f"{name} has no row in docs/configuration.md"
    return row.group(1).strip()


def test_the_configuration_reference_states_the_v2_defaults() -> None:
    assert _default_cell("OBJECT_STORAGE__BUCKET_HTML") == "`html`"
    assert _default_cell("RETRIEVAL__RERANK_MODEL") == "`cross-encoder/ms-marco-MiniLM-L-6-v2`"
    assert _default_cell("RETRIEVAL__RERANK_TIMEOUT_MS") == "`1000`"
    assert _default_cell("RETRIEVAL__RERANK_ENABLED") == "`true`"
    assert _default_cell("RETRIEVAL__CATEGORY_FILTER_ENABLED") == "`true`"
    # Honoured now: unset means the FAST tier model, so a copied .env.example must not name one.
    assert _default_cell("SUMMARIZATION__SUMMARIZER_MODEL") == "unset"


@pytest.mark.parametrize("key", V2_KEYS)
def test_every_v2_key_is_in_env_example(key: str) -> None:
    assert re.search(rf"^#?\s*{key}=", _read(".env.example"), re.M), key


@pytest.mark.parametrize("key", V2_KEYS)
def test_every_v2_key_is_in_the_configuration_reference(key: str) -> None:
    assert f"`{key}`" in _read("docs/configuration.md"), key


def test_the_gemini_embedding_example_matches_what_the_stack_env_renders() -> None:
    example = dict(re.findall(r"^# (EMBEDDING__[A-Z_]+)=(\S+)$", _read(".env.example"), re.M))
    rendered = render_stack_env(get_profile("gpt-4o-mini"), DOT_ENV)
    for name in ("EMBEDDING__MOCK", "EMBEDDING__MODEL_NAME", "EMBEDDING__BASE_URL"):
        assert example[name] == rendered[name], name
    # The key line is a <placeholder>, never a key.
    assert re.search(r"^# EMBEDDING__API_KEY=<[^>]+>$", _read(".env.example"), re.M)


def test_the_configuration_reference_documents_the_stack_only_summarizer_name() -> None:
    assert "`BENCH_SUMMARIZER_MODEL`" in _read("docs/configuration.md")


# --- what docs/demo-runbook.md §9.9 tells the owner to run ------------------------------------
# The owner runs these blocks against real quota, so the blocks are executed here: the .env of
# step 1 against the check above, the preflight probe on real settings, and the run_config
# helper of step 4 (a bash function) against stand-in executables for docker, uv and curl.


def _runbook_9_9() -> str:
    text = _read("docs/demo-runbook.md")
    start = text.index("### 9.9 ")
    end = text.find("\n## ", start)
    return text[start : end if end != -1 else len(text)]


def _fenced(section: str, language: str) -> list[str]:
    return re.findall(rf"```{language}\n(.*?)```", section, re.S)


def _block_with(section: str, language: str, marker: str) -> str:
    blocks = [block for block in _fenced(section, language) if marker in block]
    assert len(blocks) == 1, f"expected one {language} block with {marker!r} in §9.9"
    return blocks[0]


def test_the_dot_env_of_step_1_is_what_the_host_check_accepts_for_every_model() -> None:
    parsed = dotenv_values(
        stream=io.StringIO(_block_with(_runbook_9_9(), "dotenv", "EMBEDDING__MOCK")),
        interpolate=False,
    )
    keys = {
        "BENCH_OPENAI_API_KEY": OPENAI_KEY,
        "LLM__OPENAI_API_KEY": GEMINI_KEY,
        "EMBEDDING__API_KEY": GEMINI_KEY,  # "the same Gemini key"
    }
    assert set(keys) <= set(parsed), "step 1 must name the keys as placeholders"
    assert parsed["RETRIEVAL__CATEGORY_FILTER_ENABLED"] == "false"  # the benchmark's value
    environ = {name: value for name, value in parsed.items() if value is not None}
    environ.update(keys)
    for name in sorted(PROFILES):
        assert _problems(name, environ) == [], name
    # Lines the owner must not keep (an older .env has them; the fresh example does not).
    for name in HOST_ONLY:
        assert name not in parsed, f"step 1 must not set {name}"


def _probe_script() -> str:
    found = re.search(r"PROBE=\$\(cat <<'PY'\n(.*?)\nPY\n\)", _runbook_9_9(), re.S)
    assert found, "step 5 must define PROBE (the settings probe) as a heredoc"
    return found.group(1)


def _run_probe(where: Path, env: dict[str, str], *argv: str, dot_env: str | None = None) -> str:
    """Run the step 5 probe as `python -c "$PROBE" [profile]` does: a container has no .env."""
    where.mkdir(parents=True, exist_ok=True)
    if dot_env is not None:
        (where / ".env").write_text(dot_env, encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-c", _probe_script(), *argv],
        cwd=where,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(REPO_ROOT),
            # config/categories.yaml is relative to the repo root, the cwd of a real run
            "ROUTING__CATEGORIES_CONFIG_PATH": str(REPO_ROOT / "config" / "categories.yaml"),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _container_env(values: dict[str, str]) -> dict[str, str]:
    """What Compose hands a container: the rendered settings, the summarizer under its own name."""
    env = {name: value for name, value in values.items() if name != "BENCH_SUMMARIZER_MODEL"}
    env["SUMMARIZATION__SUMMARIZER_MODEL"] = values["BENCH_SUMMARIZER_MODEL"]
    return env


def test_the_preflight_probe_agrees_between_the_container_and_the_host_when_dot_env_is_right(
    tmp_path: Path,
) -> None:
    profile = get_profile("qwen2.5-7b")
    container_env = _container_env(render_stack_env(profile, HOST_ENV))
    container = _run_probe(tmp_path / "container", container_env)
    host = _run_probe(tmp_path / "host", {}, profile.name, dot_env=DOT_ENV_TEXT)
    assert host == container
    # It reads real settings: the model, the Gemini embedding, the budget and the lanes.
    for fact in (
        profile.model,
        "gemini-embedding-001",
        "1536",
        "3000",
        "category filter: False",
        "email.support.normal",
    ):
        assert fact in host, fact


def test_the_preflight_probe_shows_an_old_dot_env_as_a_difference(tmp_path: Path) -> None:
    profile = get_profile("qwen2.5-7b")
    container = _run_probe(
        tmp_path / "container", _container_env(render_stack_env(profile, HOST_ENV))
    )
    old = _dot_env(
        RETRIEVAL__RETRIEVAL_TIMEOUT_MS="500",
        RETRIEVAL__CATEGORY_FILTER_ENABLED="true",
        SUMMARIZATION__SUMMARIZER_MODEL="gpt-4o-mini",
        ROUTING__CONFIGURED_CONSUMERS='["email.support.*"]',
    )
    host = _run_probe(tmp_path / "host", {}, profile.name, dot_env=old)
    differing = {line.split(":")[0] for line in host.splitlines() if line not in container}
    assert differing == {"retrieval budget", "category filter", "summarizer", "lane queues"}


BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="the runbook's helpers are bash")
# Stand-ins for the executables run_config calls, so it runs without docker, uv, a guard-worker
# or a model. The guard-worker stand-in does what the real one does in order: it writes its pid
# file first, and answers /readyz only later, after it has "started its consumers".
STUBS = {
    "docker": """\
echo "docker $*" >> "$STUB_LOG"
case "$1" in inspect) echo healthy ;; compose) [ "$2" = ps ] && echo ai-worker-container ;; esac
exit 0
""",
    "curl": '[ -e "$READY_FLAG" ]\n',  # curl -f fails until the guard-worker is ready
    "uv": """\
args="$*"
run=$(sed -n 's/.*--run \\([^ ]*\\).*/\\1/p' <<<"$args")
config=$(sed -n 's/.*--config \\([^ ]*\\).*/\\1/p' <<<"$args")
case "$args" in
  *live.guard_worker*)
    pidfile="evaluation/results/mailguard_bench/$run/raw/guard_worker.$config.pid"
    trap 'rm -f "$pidfile"; exit 0' TERM
    sleep "${GW_PID_DELAY:-0.2}" & wait $!
    echo $$ > "$pidfile"
    sleep "${GW_READY_DELAY:-1.2}" & wait $!
    if [ "${GW_DIES:-0}" = 1 ]; then exit 3; fi
    touch "$READY_FLAG"
    while :; do sleep 0.1 & wait $!; done ;;
  *live.run*)
    if [ -e "$READY_FLAG" ]; then state=ready; else state=not-ready; fi
    echo "runner $state $config" >> "$STUB_LOG" ;;
esac
""",
}


def _run_config(tmp_path: Path, config: str, **stub_env: str) -> tuple[int, str, list[str]]:
    """Run the runbook's run_config for one config; return its exit code, stderr, the stubs' log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        executable = bin_dir / name
        executable.write_text(f"#!/usr/bin/env bash\n{body}", encoding="utf-8")
        executable.chmod(0o755)
    (tmp_path / "Makefile").write_text("MAILGUARD_COMMIT ?= 0123abc\n", encoding="utf-8")
    helpers = _block_with(_runbook_9_9(), "bash", "run_config() {")
    log = tmp_path / "calls.log"
    assert BASH is not None
    done = subprocess.run(
        [BASH, "-c", f"{helpers}\nrun_config {config}"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_LOG": str(log),
            "READY_FLAG": str(tmp_path / "ready"),
            **stub_env,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return done.returncode, done.stderr, calls


def _pid_file(tmp_path: Path, config: str) -> Path:
    run = "2026-09-29-qwen25-live"  # the RUN the helper block sets
    return (
        tmp_path / "evaluation/results/mailguard_bench" / run / "raw" / f"guard_worker.{config}.pid"
    )


@needs_bash
def test_every_bash_block_of_the_runbook_section_parses() -> None:
    blocks = _fenced(_runbook_9_9(), "bash")
    assert blocks
    assert BASH is not None
    for block in blocks:
        done = subprocess.run(
            [BASH, "-n"], input=block, capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, f"{done.stderr}\n{block}"


@needs_bash
def test_the_runner_starts_only_once_the_guard_worker_answers_readyz(tmp_path: Path) -> None:
    # The guard-worker writes its pid file before it connects and starts its consumers; the
    # runner fails a config at once when a lane queue has no consumer, so a run that starts on
    # the pid file alone loses the race whenever the runner is the faster process.
    code, stderr, calls = _run_config(tmp_path, "C3")
    assert code == 0, stderr
    assert calls == ["docker compose stop ai-worker", "runner ready C3"]
    assert not _pid_file(tmp_path, "C3").exists()  # the helper stopped the guard-worker again


@needs_bash
def test_a_guard_worker_that_dies_before_it_is_ready_fails_the_config(tmp_path: Path) -> None:
    code, stderr, calls = _run_config(tmp_path, "C3", GW_DIES="1")
    assert code == 1
    assert "FAIL guard-worker C3 exited" in stderr
    assert not [call for call in calls if call.startswith("runner")]


@needs_bash
def test_a_guard_worker_that_never_becomes_ready_is_stopped_and_fails_the_config(
    tmp_path: Path,
) -> None:
    code, stderr, calls = _run_config(tmp_path, "C3", GW_READY_DELAY="8", GW_WAIT_S="2")
    assert code == 1
    assert "FAIL guard-worker C3 not ready after 2 s" in stderr
    assert not [call for call in calls if call.startswith("runner")]
    assert not _pid_file(tmp_path, "C3").exists()  # it was sent SIGTERM, not left consuming


@needs_bash
def test_c0_switches_the_container_on_and_starts_no_guard_worker(tmp_path: Path) -> None:
    code, stderr, calls = _run_config(tmp_path, "C0")
    assert code == 0, stderr
    assert calls[0] == "docker compose start ai-worker"
    assert calls[-1] == "runner not-ready C0"  # not-ready: no stand-in guard-worker ever ran
    assert not _pid_file(tmp_path, "C0").exists()


def test_the_step_5_preflight_prints_the_floor_and_says_which_service_each_switch_matters_for() -> (
    None
):
    step5 = _runbook_9_9().split("**5. Preflight", 1)[1].split("**6.", 1)[0]
    loop = _block_with(step5, "bash", "catfilter=")
    assert "floor=${TRIAGE__CATEGORY_RETRIEVAL_FLOOR" in loop
    expected = step5.split("Every line must show", 1)[1].split("\n", 1)[0]
    assert "`floor=true`" in expected and "`catfilter=false`" in expected
    # catfilter is read by the ai-worker alone and the floor by the triage-worker alone; the
    # other services print the forwarded value, which is not evidence of their behaviour.
    assert "only the `ai-worker`'s `catfilter`" in step5
    assert "only the `triage-worker`'s `floor`" in step5
