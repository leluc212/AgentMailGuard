"""The teammate's environment doctor (task 7.23; R22.12, R24.5).

Every check is a pure function over injected inputs, so these tests feed it what a WSL2 laptop, a
native Windows box or a half-set-up machine would show. Docker, the network and the clock are never
touched: the docker CLI, the ports and the Ollama server are faked here (CLAUDE.md section 8).
A few tests use real local resources (a git repository in a temp dir, a socket, a local server).
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from evaluation.mailguard_bench.guard_env import GuardEnvError, require_pinned_worktree
from evaluation.mailguard_bench.kit import doctor, pinned
from evaluation.mailguard_bench.kit.doctor import (
    CommandResult,
    HttpResult,
    Result,
    Status,
    World,
)
from evaluation.mailguard_bench.model_profiles import get_profile

GB = 10**9
SECRET = "sk-THE-SECRET-VALUE-1234567890"
GEMINI = "AIza-THE-GEMINI-KEY-0987654321"
COMPOSE = """
services:
  postgres:
    ports:
      - "${DATABASE__PORT:-5433}:5432"
  rabbitmq:
    ports:
      - "${BROKER__PORT:-5672}:5672"
      - "15672:15672"
  api:
    ports:
      - "127.0.0.1:8000:8000"
  worker:
    image: x
"""


def ok_env_text(**extra: str) -> str:
    values = {
        "BENCH_OPENAI_API_KEY": SECRET,
        "LLM__OPENAI_API_KEY": GEMINI,
        "EMBEDDING__MOCK": "false",
        "EMBEDDING__MODEL_NAME": "gemini-embedding-001",
        "EMBEDDING__DIMENSION": "1536",
        "EMBEDDING__BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai",
        "EMBEDDING__API_KEY": GEMINI,
        "RETRIEVAL__RETRIEVAL_TIMEOUT_MS": "3000",
        "LLM__TIMEOUT_S": "60",
        **extra,
    }
    return "\n".join(f"{k}={v}" for k, v in values.items() if v != "") + "\n"


def by_check(results: Sequence[Result]) -> dict[str, Result]:
    return {r.check: r for r in results}


# --- platform, location, systemd -------------------------------------------------------------


@pytest.mark.parametrize(
    ("system", "proc", "expected"),
    [
        ("Linux", "Linux version 5.15.167.4-microsoft-standard-WSL2 (root@x)", "wsl2"),
        ("Linux", "Linux version 4.4.0-19041-Microsoft (Microsoft@Microsoft.com)", "wsl1"),
        ("Linux", "Linux version 6.8.0-45-generic (buildd@lcy02)", "linux"),
        ("Linux", None, "linux"),
        ("Windows", None, "windows"),
        ("Darwin", None, "other"),
    ],
)
def test_platform_is_told_from_the_kernel_string(
    system: str, proc: str | None, expected: str
) -> None:
    assert doctor.detect_platform(system, proc) == expected


def test_wsl2_and_linux_are_ok_and_native_windows_is_a_warning() -> None:
    assert doctor.check_platform("wsl2").status is Status.OK
    assert doctor.check_platform("linux").status is Status.OK
    windows = doctor.check_platform("windows")
    assert windows.status is Status.WARN
    assert "docs/benchmark-windows-native.md" in windows.hint


def test_wsl1_cannot_run_docker_engine() -> None:
    result = doctor.check_platform("wsl1")
    assert result.status is Status.FAIL
    assert "wsl --set-version" in result.hint


@pytest.mark.parametrize("path", ["/mnt/c/Users/me/rag-email", "/mnt/d/work/rag-email"])
def test_a_repository_on_a_windows_drive_is_a_warning_in_wsl(path: str) -> None:
    result = doctor.check_repo_location("wsl2", path)
    assert result.status is Status.WARN
    assert "~/work" in result.hint


def test_a_repository_in_the_linux_file_system_is_fine() -> None:
    assert doctor.check_repo_location("wsl2", "/home/me/work/rag-email").status is Status.OK
    assert doctor.check_repo_location("linux", "/mnt/data/rag-email").status is Status.OK


def test_wsl_without_systemd_is_a_warning() -> None:
    assert doctor.check_systemd("wsl2", systemd_running=False).status is Status.WARN
    assert doctor.check_systemd("wsl2", systemd_running=True).status is Status.OK
    assert doctor.check_systemd("linux", systemd_running=False).status is Status.OK


# --- line endings -----------------------------------------------------------------------------

EOL_CLEAN = (
    "i/lf    w/lf    attr/text=auto eol=lf \tMakefile\n"
    "i/crlf  w/crlf  attr/-text             \ttests/fixtures/mime/04.eml\n"
    "i/-text w/-text attr/-text             \tfiles.zip\n"
)
EOL_DIRTY = EOL_CLEAN + (
    "i/lf    w/crlf  attr/text=auto eol=lf \tscripts/run.sh\n"
    "i/lf    w/mixed attr/text=auto eol=lf \tdocs/a.md\n"
)


def test_no_crlf_in_text_files_is_ok() -> None:
    assert doctor.check_line_endings(CommandResult(0, EOL_CLEAN, "")).status is Status.OK


def test_crlf_in_text_files_is_a_failure_that_names_them() -> None:
    result = doctor.check_line_endings(CommandResult(0, EOL_DIRTY, ""))
    assert result.status is Status.FAIL
    assert "2 " in result.detail and "scripts/run.sh" in result.detail
    assert "clone" in result.hint and "autocrlf" in result.hint


def test_line_ending_check_needs_git() -> None:
    assert doctor.check_line_endings(None).status is Status.FAIL
    assert doctor.check_line_endings(CommandResult(128, "", "not a git repository")).status is (
        Status.FAIL
    )


# --- docker -----------------------------------------------------------------------------------

INFO_OK = json.dumps(
    {"MemTotal": 12 * GB, "DockerRootDir": "/var/lib/docker", "ServerVersion": "27"}
)


def test_docker_cli_missing_is_a_failure_with_the_install_hint() -> None:
    result = doctor.check_docker_cli(None)
    assert result.status is Status.FAIL
    assert "docs.docker.com/engine/install/ubuntu" in result.hint


def test_docker_cli_present() -> None:
    result = doctor.check_docker_cli(CommandResult(0, "Docker version 27.3.1, build ce12230\n", ""))
    assert result.status is Status.OK
    assert "27.3.1" in result.detail


def test_daemon_reachable() -> None:
    assert doctor.check_docker_daemon(CommandResult(0, INFO_OK, "")).status is Status.OK


def test_daemon_permission_denied_points_at_the_docker_group() -> None:
    result = doctor.check_docker_daemon(
        CommandResult(
            1, "", "permission denied while trying to connect to the Docker daemon socket"
        )
    )
    assert result.status is Status.FAIL
    assert "usermod -aG docker" in result.hint


def test_daemon_not_running_points_at_systemctl() -> None:
    result = doctor.check_docker_daemon(
        CommandResult(1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock")
    )
    assert result.status is Status.FAIL
    assert "systemctl" in result.hint


def test_compose_v2_is_required() -> None:
    assert doctor.check_compose(CommandResult(0, "2.29.7\n", "")).status is Status.OK
    assert doctor.check_compose(CommandResult(0, "v2.29.7\n", "")).status is Status.OK
    old = doctor.check_compose(CommandResult(0, "1.29.2\n", ""))
    assert old.status is Status.FAIL
    assert doctor.check_compose(
        CommandResult(1, "", "docker: 'compose' is not a docker command")
    ).status is (Status.FAIL)
    assert doctor.check_compose(None).status is Status.FAIL


def test_docker_memory_needs_eight_gigabytes() -> None:
    assert doctor.check_docker_memory(doctor.parse_docker_info(INFO_OK)).status is Status.OK
    small = doctor.check_docker_memory(doctor.parse_docker_info(json.dumps({"MemTotal": 4 * GB})))
    assert small.status is Status.FAIL
    assert ".wslconfig" in small.hint
    assert "4.0 GB" in small.detail


def test_docker_memory_unknown_when_the_daemon_is_unreachable() -> None:
    assert doctor.check_docker_memory(None).status is Status.FAIL


def test_docker_info_that_is_not_json_is_ignored() -> None:
    assert doctor.parse_docker_info("not json") is None


# --- disk, tools, python ----------------------------------------------------------------------


def test_disk_below_25_gb_is_a_warning() -> None:
    assert doctor.check_disk("repository disk", "/home/x", 60 * GB).status is Status.OK
    low = doctor.check_disk("repository disk", "/home/x", 10 * GB)
    assert low.status is Status.WARN
    assert "10.0 GB" in low.detail and "25" in low.detail


def test_disk_that_cannot_be_measured_is_a_warning_not_a_pass() -> None:
    result = doctor.check_disk("Docker disk", "/var/lib/docker", None)
    assert result.status is Status.WARN
    assert "cannot be measured" in result.detail
    assert result.hint


def test_wsl2_also_measures_the_windows_drive_that_holds_the_virtual_disk() -> None:
    # the ext4 disk reports its virtual capacity (about 1 TB); the C: drive is the real limit
    def free(path: str) -> int | None:
        return {"/mnt/c": 12 * GB}.get(path, 900 * GB)

    results = by_check(
        doctor.run_checks(good_world(free_bytes=free), model_profile="gpt-4o-mini", reader=None)
    )
    windows = results["windows drive (C:)"]
    assert windows.status is Status.WARN
    assert "12.0 GB" in windows.detail and "/mnt/c" in windows.detail
    assert results["repository disk"].status is Status.OK


def test_wsl2_without_a_mounted_windows_drive_skips_that_check() -> None:
    results = by_check(
        doctor.run_checks(
            good_world(free_bytes=lambda p: None if p == "/mnt/c" else 900 * GB),
            model_profile="gpt-4o-mini",
            reader=None,
        )
    )
    assert "windows drive (C:)" not in results


def test_a_linux_machine_has_no_windows_drive_check() -> None:
    world = good_world(proc_version="Linux version 6.8.0-45-generic", free_bytes=lambda p: 50 * GB)
    assert "windows drive (C:)" not in by_check(
        doctor.run_checks(world, model_profile="gpt-4o-mini", reader=None)
    )


def test_tool_present_or_missing() -> None:
    assert doctor.check_tool("uv", "/usr/bin/uv", "install uv").status is Status.OK
    missing = doctor.check_tool("uv", None, "install uv")
    assert missing.status is Status.FAIL and missing.hint == "install uv"
    assert doctor.check_tool("make", None, "x", missing_status=Status.WARN).status is Status.WARN


def test_python_312_or_newer() -> None:
    assert doctor.check_python((3, 12, 3)).status is Status.OK
    assert doctor.check_python((3, 13, 0)).status is Status.OK
    assert doctor.check_python((3, 11, 9)).status is Status.FAIL


# --- .env and the shell ----------------------------------------------------------------------


def test_env_file_present_or_missing() -> None:
    assert doctor.check_env_file(True).status is Status.OK
    missing = doctor.check_env_file(False)
    assert missing.status is Status.FAIL and "cp .env.example .env" in missing.hint


def test_keys_are_reported_by_name_only() -> None:
    environ = doctor.merge_env(ok_env_text(), {})
    results = doctor.check_env_keys(environ, get_profile("gpt-4o-mini"))
    by = by_check(results)
    assert by["env BENCH_OPENAI_API_KEY"].detail == "set"
    assert by["env LLM__OPENAI_API_KEY"].detail == "set"
    assert by["env EMBEDDING__API_KEY"].detail == "set"
    assert all(r.status is Status.OK for r in results)
    rendered = "\n".join(line for r in results for line in r.lines())
    assert SECRET not in rendered and GEMINI not in rendered


def test_a_missing_key_is_a_failure_that_names_it() -> None:
    environ = doctor.merge_env(ok_env_text(BENCH_OPENAI_API_KEY=""), {})
    by = by_check(doctor.check_env_keys(environ, get_profile("gpt-4o-mini")))
    assert by["env BENCH_OPENAI_API_KEY"].status is Status.FAIL
    assert by["env BENCH_OPENAI_API_KEY"].detail == "not set"


def test_the_profile_key_is_not_required_for_a_local_model() -> None:
    environ = doctor.merge_env(ok_env_text(BENCH_OPENAI_API_KEY=""), {})
    results = doctor.check_env_keys(environ, get_profile("qwen2.5-7b"))
    assert [r for r in results if r.status is Status.FAIL] == []
    assert "env BENCH_OPENAI_API_KEY" not in by_check(results)


def test_without_a_profile_every_profile_key_is_listed_and_a_missing_one_warns() -> None:
    environ = doctor.merge_env(ok_env_text(BENCH_OPENAI_API_KEY=""), {})
    results = by_check(doctor.check_env_keys(environ, None))
    assert results["env BENCH_OPENAI_API_KEY"].status is Status.WARN
    assert "gpt-4o-mini" in results["env BENCH_OPENAI_API_KEY"].detail


def test_the_shell_wins_over_the_env_file_when_merging() -> None:
    merged = doctor.merge_env("A=file\nB=file\n", {"B": "shell"})
    assert merged == {"A": "file", "B": "shell"}


def test_settings_that_agree_with_the_containers_pass() -> None:
    environ = doctor.merge_env(ok_env_text(), {})
    assert doctor.check_env_settings(environ, get_profile("gpt-4o-mini")).status is Status.OK


def test_wrong_settings_are_named_with_what_they_must_be_and_no_secret() -> None:
    environ = doctor.merge_env(
        ok_env_text(
            **{
                "RETRIEVAL__RETRIEVAL_TIMEOUT_MS": "500",
                "EMBEDDING__MOCK": "true",
                "EMBEDDING__API_KEY": "another-key-value-xyz",
            }
        ),
        {},
    )
    result = doctor.check_env_settings(environ, get_profile("gpt-4o-mini"))
    assert result.status is Status.FAIL
    assert "RETRIEVAL__RETRIEVAL_TIMEOUT_MS must be 3000" in result.detail
    assert "EMBEDDING__MOCK must be false" in result.detail
    assert "EMBEDDING__API_KEY must be the Gemini key" in result.detail
    assert "another-key-value-xyz" not in "\n".join(result.lines())
    assert GEMINI not in "\n".join(result.lines())


def test_a_missing_embedding_line_is_a_failure() -> None:
    environ = doctor.merge_env(ok_env_text(EMBEDDING__DIMENSION=""), {})
    result = doctor.check_env_settings(environ, None)
    assert result.status is Status.FAIL and "EMBEDDING__DIMENSION" in result.detail


def test_settings_cannot_be_compared_without_the_gemini_key() -> None:
    environ = doctor.merge_env(ok_env_text(LLM__OPENAI_API_KEY=""), {})
    result = doctor.check_env_settings(environ, get_profile("gpt-4o-mini"))
    assert result.status is Status.FAIL and "LLM__OPENAI_API_KEY" in result.detail


def test_exported_setting_prefixes_fail_by_name() -> None:
    shell = {
        "LLM__OPENAI_API_KEY": SECRET,
        "EMBEDDING__MOCK": "false",
        "BENCH_SUMMARIZER_MODEL": "m",
        "ROUTING__CONFIGURED_CONSUMERS": "[]",
        "PATH": "/usr/bin",
        "BENCH_OPENAI_API_KEY": SECRET,
    }
    result = doctor.check_shell_exports(shell)
    assert result.status is Status.FAIL
    for name in (
        "LLM__OPENAI_API_KEY",
        "EMBEDDING__MOCK",
        "BENCH_SUMMARIZER_MODEL",
        "ROUTING__CONFIGURED_CONSUMERS",
    ):
        assert name in result.detail
    assert "PATH" not in result.detail and "BENCH_OPENAI_API_KEY" not in result.detail
    assert SECRET not in "\n".join(result.lines())
    assert "unset" in result.hint


def test_a_clean_shell_passes() -> None:
    assert doctor.check_shell_exports({"PATH": "/usr/bin"}).status is Status.OK


def test_old_env_lines_are_found_by_name() -> None:
    text = (
        ok_env_text()
        + "SUMMARIZATION__SUMMARIZER_MODEL=gpt-4o-mini\nROUTING__CONFIGURED_CONSUMERS=[]\n"
    )
    result = doctor.check_old_env_lines(text)
    assert result.status is Status.FAIL
    assert "SUMMARIZATION__SUMMARIZER_MODEL" in result.detail
    assert "ROUTING__CONFIGURED_CONSUMERS" in result.detail
    assert "remove" in result.hint


def test_commented_old_lines_are_not_found() -> None:
    text = ok_env_text() + "# SUMMARIZATION__SUMMARIZER_MODEL=gpt-4o-mini\n"
    assert doctor.check_old_env_lines(text).status is Status.OK
    assert doctor.check_old_env_lines(None).status is Status.OK


# --- the guard and the pinned inputs ---------------------------------------------------------


def _git_repo(path: Path) -> str:
    path.mkdir()
    env = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "f").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(path), *env, "commit", "-q", "-m", "c"], check=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_the_guard_check_calls_guard_envs_own_function(tmp_path: Path) -> None:
    commit = _git_repo(tmp_path / "guard")
    ok = doctor.check_guard(require_pinned_worktree, tmp_path / "guard", commit)
    assert ok.status is Status.OK and commit[:8] in ok.detail
    wrong = doctor.check_guard(require_pinned_worktree, tmp_path / "guard", "0" * 40)
    assert wrong.status is Status.FAIL and "pinned commit" in wrong.detail
    # the worktree is created by `make mailguard-worktree`; `make bench-setup` only checks it
    assert "make mailguard-worktree" in wrong.hint
    assert "make bench-setup" not in wrong.hint
    assert "benchmark-windows-native.md" in wrong.hint
    (tmp_path / "guard" / "f").write_text("changed", encoding="utf-8")
    dirty = doctor.check_guard(require_pinned_worktree, tmp_path / "guard", commit)
    assert dirty.status is Status.FAIL and "uncommitted" in dirty.detail
    absent = doctor.check_guard(require_pinned_worktree, tmp_path / "nope", commit)
    assert absent.status is Status.FAIL


def test_guard_location_defaults() -> None:
    root = Path("/w/rag-email")
    assert doctor.default_guard_dir(root, {}) == Path("/w/AgentMailGuard-bench")
    assert doctor.default_guard_dir(root, {"MAILGUARD_DIR": "/x/guard"}) == Path("/x/guard")


def test_guard_location_prefers_the_subtree_when_it_exists(tmp_path: Path) -> None:
    (tmp_path / "agentmailguard").mkdir()
    assert doctor.default_guard_dir(tmp_path, {}) == tmp_path / "agentmailguard"


def test_pinned_problems_become_one_failure_each_named() -> None:
    assert doctor.check_pinned([]).status is Status.OK
    result = doctor.check_pinned(["cases.jsonl is missing", "x has sha256 a, pinned b"])
    assert result.status is Status.FAIL
    assert "cases.jsonl is missing" in result.detail and "sha256 a" in result.detail


def test_scikit_learn_version_is_a_failure_with_the_reason() -> None:
    assert doctor.check_sklearn("1.9.1").status is Status.OK
    bad = doctor.check_sklearn("1.8.0")
    assert bad.status is Status.FAIL and "unpickl" in bad.detail
    assert doctor.check_sklearn(None).status is Status.FAIL


# --- ports ------------------------------------------------------------------------------------


def test_published_ports_come_from_the_compose_file_with_env_defaults() -> None:
    ports = doctor.published_ports(COMPOSE, {})
    assert ports == {5433: "postgres", 5672: "rabbitmq", 15672: "rabbitmq", 8000: "api"}


def test_an_overridden_port_variable_is_honoured() -> None:
    ports = doctor.published_ports(COMPOSE, {"DATABASE__PORT": "6543"})
    assert 6543 in ports and 5433 not in ports


COMPOSE_PS_ARRAY = json.dumps(
    [{"Service": "postgres", "Publishers": [{"PublishedPort": 5433, "TargetPort": 5432}]}]
)
COMPOSE_PS_LINES = (
    json.dumps({"Service": "postgres", "Publishers": [{"PublishedPort": 5433}]})
    + "\n"
    + json.dumps({"Service": "api", "Publishers": [{"PublishedPort": 0}, {"PublishedPort": 8000}]})
    + "\n"
)


def test_compose_ps_output_in_both_shapes_gives_the_held_ports() -> None:
    assert doctor.parse_compose_ps(COMPOSE_PS_ARRAY) == {5433}
    assert doctor.parse_compose_ps(COMPOSE_PS_LINES) == {5433, 8000}
    assert doctor.parse_compose_ps("") == set()
    assert doctor.parse_compose_ps("garbage") == set()


def test_free_ports_and_ports_held_by_this_project_are_fine() -> None:
    result = doctor.check_ports(
        {5433: "postgres", 8000: "api"}, owned={8000}, in_use=lambda port: port == 8000
    )
    assert result.status is Status.OK


def test_a_port_held_by_something_else_fails_with_the_service_that_needs_it() -> None:
    result = doctor.check_ports(
        {5433: "postgres", 8000: "api"}, owned=set(), in_use=lambda port: port == 5433
    )
    assert result.status is Status.FAIL
    assert "5433" in result.detail and "postgres" in result.detail
    assert "8000" not in result.detail
    assert "ss -ltnp" in result.hint


def test_port_in_use_sees_a_real_listener() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert doctor.port_in_use(port) is True
    assert doctor.port_in_use(port) is False


# --- local models (Ollama) ---------------------------------------------------------------------


def _fetch(routes: Mapping[str, HttpResult | None]) -> Callable[[str], HttpResult | None]:
    def fetch(url: str) -> HttpResult | None:
        return routes.get(url)

    return fetch


TAGS = json.dumps({"models": [{"name": "qwen2.5:7b-instruct"}, {"name": "llama3.1:8b"}]})


def test_a_remote_profile_needs_no_ollama_check() -> None:
    result = doctor.check_ollama(get_profile("gpt-4o-mini"), {}, "wsl2", _fetch({}))
    assert result is None


def test_ollama_answering_and_listing_the_model_is_ok() -> None:
    url = "http://172.17.0.1:11434"
    result = doctor.check_ollama(
        get_profile("qwen2.5-7b"),
        {"BENCH_OLLAMA_BASE_URL": f"{url}/v1"},
        "wsl2",
        _fetch(
            {
                f"{url}/api/version": HttpResult(200, '{"version":"0.12.3"}'),
                f"{url}/api/tags": HttpResult(200, TAGS),
            }
        ),
    )
    assert result is not None and result.status is Status.OK
    assert "0.12.3" in result.detail and "qwen2.5:7b-instruct" in result.detail


def test_ollama_not_answering_is_a_failure_with_the_bridge_hint() -> None:
    result = doctor.check_ollama(
        get_profile("qwen2.5-7b"),
        {"BENCH_OLLAMA_BASE_URL": "http://172.17.0.1:11434/v1"},
        "wsl2",
        _fetch({}),
    )
    assert result is not None and result.status is Status.FAIL
    assert "172.17.0.1:11434" in result.detail
    assert "docs/BENCHMARK.md" in result.hint


def test_a_model_that_is_not_pulled_is_a_failure_naming_the_pull_command() -> None:
    url = "http://localhost:11434"
    result = doctor.check_ollama(
        get_profile("llama-3.1-8b-local"),
        {},
        "linux",
        _fetch(
            {
                f"{url}/api/version": HttpResult(200, '{"version":"0.12.3"}'),
                f"{url}/api/tags": HttpResult(200, json.dumps({"models": [{"name": "other:1b"}]})),
            }
        ),
    )
    assert result is not None and result.status is Status.FAIL
    assert "ollama pull llama3.1:8b" in result.hint


def test_loopback_ollama_warns_that_containers_cannot_reach_it_on_docker_engine() -> None:
    url = "http://localhost:11434"
    result = doctor.check_ollama(
        get_profile("qwen2.5-7b"),
        {},
        "wsl2",
        _fetch(
            {
                f"{url}/api/version": HttpResult(200, '{"version":"0.12.3"}'),
                f"{url}/api/tags": HttpResult(200, TAGS),
            }
        ),
    )
    assert result is not None and result.status is Status.WARN
    assert "host.docker.internal" in result.detail
    assert "BENCH_OLLAMA_BASE_URL" in result.hint


@pytest.mark.parametrize("host", ["[::1]", "127.0.0.1", "127.1.2.3", "localhost"])
def test_every_loopback_spelling_warns_on_docker_engine(host: str) -> None:
    url = f"http://{host}:11434"
    result = doctor.check_ollama(
        get_profile("qwen2.5-7b"),
        {"BENCH_OLLAMA_BASE_URL": f"{url}/v1"},
        "linux",
        _fetch(
            {
                f"{url}/api/version": HttpResult(200, '{"version":"0.12.3"}'),
                f"{url}/api/tags": HttpResult(200, TAGS),
            }
        ),
    )
    assert result is not None and result.status is Status.WARN, host


def test_a_bridge_address_is_not_loopback() -> None:
    url = "http://172.17.0.1:11434"
    result = doctor.check_ollama(
        get_profile("qwen2.5-7b"),
        {"BENCH_OLLAMA_BASE_URL": f"{url}/v1"},
        "wsl2",
        _fetch(
            {
                f"{url}/api/version": HttpResult(200, '{"version":"0.12.3"}'),
                f"{url}/api/tags": HttpResult(200, TAGS),
            }
        ),
    )
    assert result is not None and result.status is Status.OK


def test_a_tagless_model_name_matches_its_latest_tag() -> None:
    assert doctor.model_listed("llama3.1", ["llama3.1:latest"])
    assert doctor.model_listed("llama3.1:8b", ["llama3.1:8b"])
    assert not doctor.model_listed("llama3.1:8b", ["llama3.1:70b"])


def test_is_local_endpoint() -> None:
    for url in (
        "http://localhost:11434/v1",
        "http://127.0.0.1:11434/v1",
        "http://172.17.0.1:11434/v1",
        "http://host.docker.internal:11434/v1",
        "http://192.168.1.20:11434/v1",
    ):
        assert doctor.is_local_endpoint(url), url
    for url in ("https://api.openai.com/v1", "https://openrouter.ai/api/v1"):
        assert not doctor.is_local_endpoint(url), url


def test_the_http_helper_reads_a_real_local_server() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b'{"version":"9"}' if self.path == "/api/version" else b"nope"
            self.send_response(200 if self.path == "/api/version" else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        assert doctor.http_get(f"{base}/api/version") == HttpResult(200, '{"version":"9"}')
        missing = doctor.http_get(f"{base}/other")
        assert missing is not None and missing.status == 404
    finally:
        server.shutdown()
        server.server_close()
    assert doctor.http_get(f"{base}/api/version") is None


def test_gpu_is_info_and_a_warning_only_for_a_local_model_without_one() -> None:
    assert doctor.check_gpu("/usr/lib/wsl/lib/nvidia-smi", local_model=True).status is Status.OK
    assert doctor.check_gpu(None, local_model=False).status is Status.OK
    assert doctor.check_gpu(None, local_model=None).status is Status.OK
    warn = doctor.check_gpu(None, local_model=True)
    assert warn.status is Status.WARN and "CPU" in warn.detail


# --- the reader --------------------------------------------------------------------------------


def test_a_benchmarked_model_may_not_be_the_reader() -> None:
    for model in ("gpt-4o-mini", "qwen2.5:7b-instruct", "llama3.1:8b"):
        result = doctor.check_reader(model, get_profile("gpt-4o-mini"))
        assert result.status is Status.FAIL, model
        assert "benchmarked model" in result.detail


@pytest.mark.parametrize(
    "variant",
    [
        "qwen2.5:7b",
        "Qwen2.5-7B-Instruct",
        "qwen2.5:7b-instruct-q4_K_M",
        "llama3.1:8b-instruct-q4_0",
        "Llama-3.1-8B-Instruct",
        "meta-llama/Llama-3.1-8B-Instruct",
        "gpt-4o-mini-2024-07-18",
        "openai/gpt-4o-mini",
        "GPT-4o-mini",
    ],
)
def test_a_variant_of_a_benchmarked_model_may_not_read_either(variant: str) -> None:
    # ADR-0012 decision 7 is about the model, not the string that names it
    result = doctor.check_reader(variant, get_profile("gpt-4o-mini"))
    assert result.status is Status.FAIL, variant
    assert "benchmarked model" in result.detail


@pytest.mark.parametrize(
    "other",
    ["gpt-4o", "gpt-4.1-mini", "qwen2.5:14b", "llama3.1:70b", "llama3.2:3b", "gemini-2.5-flash"],
)
def test_a_different_model_of_the_same_family_may_read(other: str) -> None:
    assert doctor.check_reader(other, get_profile("gpt-4o-mini")).status is Status.OK, other


def test_another_model_may_read() -> None:
    assert doctor.check_reader("gemini-2.5-flash", get_profile("gpt-4o-mini")).status is Status.OK


def test_no_reader_yet_is_a_warning_that_says_to_choose_one() -> None:
    result = doctor.check_reader(None, None)
    assert result.status is Status.WARN and "ADR-0012" in result.hint


# --- the classifier directory the runners read ------------------------------------------------

PINNED_SHA = pinned.PINNED.classifier.sha256
JOBLIB = "l1_injection_clf_v1.joblib"
ROOT = Path("/home/u/work/rag-email")
MAKEFILE_TODAY = "MAILGUARD_ARTIFACTS ?= $(abspath $(CURDIR)/../AgentMailGuard-bench-artifacts)\n"
MAKEFILE_R7 = "MAILGUARD_ARTIFACTS ?= $(CURDIR)/evaluation/mailguard_bench/pinned\n"


def _sha_table(table: Mapping[str, str]) -> Callable[[Path], str | None]:
    return lambda path: table.get(str(path))


def test_the_runners_read_the_classifier_where_mailguard_artifacts_points() -> None:
    folder = "/home/u/work/rag-email/evaluation/mailguard_bench/pinned"
    result = doctor.check_artifacts(
        {"MAILGUARD_ARTIFACTS": folder},
        MAKEFILE_TODAY,
        ROOT,
        _sha_table({f"{folder}/{JOBLIB}": PINNED_SHA}),
    )
    assert result.status is Status.OK
    assert folder in result.detail  # the effective directory is printed


def test_a_classifier_with_another_hash_is_a_failure() -> None:
    folder = "/elsewhere/artifacts"
    result = doctor.check_artifacts(
        {"MAILGUARD_ARTIFACTS": folder},
        MAKEFILE_TODAY,
        ROOT,
        _sha_table({f"{folder}/{JOBLIB}": "0" * 64}),
    )
    assert result.status is Status.FAIL
    assert folder in result.detail and "0" * 64 in result.detail
    assert "export MAILGUARD_ARTIFACTS" in result.hint


def test_the_makefile_default_is_used_when_the_shell_sets_nothing() -> None:
    # today's Makefile points next to the repository, where a teammate has nothing
    result = doctor.check_artifacts({}, MAKEFILE_TODAY, ROOT, _sha_table({}))
    assert result.status is Status.FAIL
    assert "/home/u/work/AgentMailGuard-bench-artifacts" in result.detail
    assert "missing" in result.detail
    assert "evaluation/mailguard_bench/pinned" in result.hint


def test_the_single_repo_makefile_default_is_the_pinned_folder() -> None:
    folder = f"{ROOT}/evaluation/mailguard_bench/pinned"
    result = doctor.check_artifacts(
        {}, MAKEFILE_R7, ROOT, _sha_table({f"{folder}/{JOBLIB}": PINNED_SHA})
    )
    assert result.status is Status.OK and folder in result.detail


def test_a_makefile_default_that_cannot_be_read_is_a_warning() -> None:
    for makefile in (None, "", "MAILGUARD_ARTIFACTS ?= $(if $(X),a,b)\n"):
        result = doctor.check_artifacts({}, makefile, ROOT, _sha_table({}))
        assert result.status is Status.WARN, makefile
        assert "export MAILGUARD_ARTIFACTS" in result.hint


def test_the_shell_wins_over_the_makefile_as_make_does() -> None:
    folder = "/mine"
    result = doctor.check_artifacts(
        {"MAILGUARD_ARTIFACTS": folder},
        MAKEFILE_R7,
        ROOT,
        _sha_table({f"{folder}/{JOBLIB}": PINNED_SHA}),
    )
    assert result.status is Status.OK and folder in result.detail


def test_the_real_sha256_reader_returns_none_for_a_missing_file(tmp_path: Path) -> None:
    assert doctor._file_sha256(tmp_path / "nope") is None
    (tmp_path / "f").write_bytes(b"abc")
    assert doctor._file_sha256(tmp_path / "f") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_a_byte_order_mark_in_env_does_not_hide_keys_or_old_lines() -> None:
    text = "﻿" + ok_env_text(SUMMARIZATION__SUMMARIZER_MODEL="gpt-4o-mini")
    world = good_world(read_text=lambda p: text if p.name == ".env" else COMPOSE)
    results = by_check(doctor.run_checks(world, model_profile="gpt-4o-mini", reader=None))
    assert results["env BENCH_OPENAI_API_KEY"].status is Status.OK
    assert results["old .env lines"].status is Status.FAIL
    assert results[".env encoding"].status is Status.WARN
    assert "BOM" in results[".env encoding"].detail


# --- output and the whole run ------------------------------------------------------------------


def test_result_lines_carry_status_check_detail_and_the_fix() -> None:
    assert Result(Status.OK, "uv", "found").lines() == ["ok uv: found"]
    assert Result(Status.FAIL, "uv", "missing", "install it").lines() == [
        "FAIL uv: missing",
        "     fix: install it",
    ]
    assert Result(Status.WARN, "disk", "low", "free space").lines()[0] == "WARN disk: low"


def good_world(**overrides: object) -> World:
    def run(cmd: Sequence[str]) -> CommandResult | None:
        joined = " ".join(cmd)
        table = {
            "git ls-files --eol": CommandResult(0, EOL_CLEAN, ""),
            "docker --version": CommandResult(0, "Docker version 27.3.1, build x\n", ""),
            "docker info --format {{json .}}": CommandResult(0, INFO_OK, ""),
            "docker compose version --short": CommandResult(0, "2.29.7\n", ""),
            "docker compose ps --format json": CommandResult(0, "", ""),
        }
        return table.get(joined)

    files = {"docker-compose.yml": COMPOSE, ".env": ok_env_text()}
    base = {
        "system": "Linux",
        "proc_version": "Linux version 5.15-microsoft-standard-WSL2",
        "repo_root": Path("/home/u/work/rag-email"),
        "environ": {
            "PATH": "/usr/bin",
            "MAILGUARD_ARTIFACTS": "/home/u/work/rag-email/evaluation/mailguard_bench/pinned",
        },
        "run": run,
        "which": lambda name: f"/usr/bin/{name}",
        "free_bytes": lambda path: 100 * GB,
        "port_in_use": lambda port: False,
        "http_get": _fetch({}),
        "read_text": lambda path: files.get(path.name),
        "systemd_running": True,
        "python_version": (3, 12, 3),
        "installed_sklearn": "1.9.1",
        "require_guard": lambda path, commit: None,
        "pinned_problems": lambda: [],
        "file_sha256": lambda path: PINNED_SHA,
    }
    base.update(overrides)
    return World(**base)  # type: ignore[arg-type]


def test_a_good_machine_passes_every_check() -> None:
    results = doctor.run_checks(
        good_world(), model_profile="gpt-4o-mini", reader="gemini-2.5-flash"
    )
    failing = [r for r in results if r.status is not Status.OK]
    assert failing == [], [r.lines() for r in failing]
    checks = {r.check for r in results}
    for expected in (
        "platform",
        "repository location",
        "line endings",
        "docker CLI",
        "docker daemon",
        "docker compose",
        "docker memory",
        "repository disk",
        "uv",
        "python",
        ".env",
        "exported settings",
        "old .env lines",
        "AgentMailGuard",
        "pinned inputs",
        "classifier directory",
        "scikit-learn",
        "ports",
        "reader model",
    ):
        assert expected in checks, expected


def test_the_checks_run_for_the_named_profile_only() -> None:
    results = doctor.run_checks(good_world(), model_profile="gpt-4o-mini", reader=None)
    assert "ollama" not in {r.check for r in results}


def test_a_local_profile_adds_the_ollama_and_gpu_checks() -> None:
    results = doctor.run_checks(good_world(), model_profile="qwen2.5-7b", reader="x-reader")
    assert {"ollama", "nvidia-smi"} <= {r.check for r in results}


def test_a_broken_machine_reports_each_problem_and_leaks_no_secret() -> None:
    world = good_world(
        environ={"PATH": "/usr/bin", "LLM__OPENAI_API_KEY": SECRET, "RETRIEVAL__X": "1"},
        run=lambda cmd: None,
        which=lambda name: None,
        free_bytes=lambda path: 5 * GB,
        port_in_use=lambda port: True,
        pinned_problems=lambda: ["cases.jsonl is missing"],
        installed_sklearn="1.2.0",
        python_version=(3, 10, 0),
        systemd_running=False,
        read_text=lambda path: (
            ok_env_text(SUMMARIZATION__SUMMARIZER_MODEL="gpt-4o-mini")
            if path.name == ".env"
            else COMPOSE
        ),
    )

    def refuse(path: Path, commit: str) -> None:
        raise GuardEnvError(f"worktree {path} not found")

    world = good_world(**{**world.__dict__, "require_guard": refuse})
    results = doctor.run_checks(world, model_profile="gpt-4o-mini", reader="gpt-4o-mini")
    statuses = by_check(results)
    for name in (
        "docker CLI",
        "docker daemon",
        "uv",
        "python",
        "exported settings",
        "old .env lines",
        "AgentMailGuard",
        "pinned inputs",
        "scikit-learn",
        "ports",
        "reader model",
        "line endings",
    ):
        assert statuses[name].status is Status.FAIL, name
    assert statuses["repository disk"].status is Status.WARN
    rendered = "\n".join(line for r in results for line in r.lines())
    assert SECRET not in rendered and GEMINI not in rendered
    assert SECRET[:6] not in rendered


def test_main_exits_one_on_a_failure_and_zero_otherwise(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert doctor.main(["--model-profile", "gpt-4o-mini"], world=good_world()) == 0
    out = capsys.readouterr().out
    assert "ok platform:" in out
    assert (
        doctor.main(["--model-profile", "gpt-4o-mini"], world=good_world(which=lambda n: None)) == 1
    )
    assert "FAIL uv:" in capsys.readouterr().out


def test_an_unknown_profile_is_a_failure_line_not_a_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert doctor.main(["--model-profile", "no-such-model"], world=good_world()) == 1
    out = capsys.readouterr().out
    assert "FAIL model profile:" in out and "no-such-model" in out
