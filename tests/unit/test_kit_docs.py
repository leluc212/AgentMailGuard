"""The teammate's guides say what the code does (task 7.23).

A guide that names a key, a port, a profile or a flag that the code has dropped sends the teammate
on a dead end on a machine nobody else can debug. These tests read the guides and compare them
with the code they describe: the .env block with the stack env's constants, the profile names with
the profile table, the ports with the compose file, the doctor's flags with its parser.
"""

from __future__ import annotations

import importlib
import re
import shlex
from collections.abc import Sequence

from evaluation.mailguard_bench.guard_env import (
    REPO_ROOT,
    V2_MAILGUARD_COMMIT,
    V2_MAILGUARD_TREE,
    GuardEnvError,
)
from evaluation.mailguard_bench.kit import doctor, pinned
from evaluation.mailguard_bench.kit.doctor import HttpResult
from evaluation.mailguard_bench.live import stack_env
from evaluation.mailguard_bench.live.guard_worker import DEFAULT_PORT
from evaluation.mailguard_bench.model_profiles import PROFILES

GUIDE = (REPO_ROOT / "docs" / "BENCHMARK.md").read_text(encoding="utf-8")
NATIVE = (REPO_ROOT / "docs" / "benchmark-windows-native.md").read_text(encoding="utf-8")


def _fenced(text: str, language: str) -> list[str]:
    return re.findall(rf"```{language}\n(.*?)```", text, flags=re.DOTALL)


def _dotenv_values(block: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in block.splitlines():
        name, _, rest = line.partition("=")
        if name and not name.startswith("#"):
            values[name] = rest.split("#")[0].strip()
    return values


def _with_keys(values: dict[str, str]) -> dict[str, str]:
    """The guide's lines with each ``<placeholder>`` replaced by a stand-in key."""
    return {k: (f"key-for-{k}" if v.startswith("<") else v) for k, v in values.items()}


def test_the_env_block_is_the_runbooks_and_agrees_with_the_stack_env() -> None:
    [block] = [b for b in _fenced(GUIDE, "dotenv") if "EMBEDDING__MOCK" in b]
    values = _dotenv_values(block)
    assert set(values) == {
        "BENCH_OPENAI_API_KEY",
        "BENCH_OPENROUTER_API_KEY",
        *stack_env.HOST_MUST_SET,
    }
    assert values["EMBEDDING__MOCK"] == "false"
    assert values["EMBEDDING__DIMENSION"] == str(stack_env.EMBEDDING_DIMENSION)
    assert values["RETRIEVAL__RETRIEVAL_TIMEOUT_MS"] == str(stack_env.RETRIEVAL_TIMEOUT_MS)
    assert float(values["LLM__TIMEOUT_S"]) == stack_env.DEFAULT_LLM_TIMEOUT_S
    environ = _with_keys(values)
    # Every profile the guide runs (the Gemma test profile needs a key the guide does not ask for).
    for name, profile in PROFILES.items():
        if profile.api_key_env is not None and profile.api_key_env not in values:
            continue
        rendered = stack_env.render_stack_env(profile, environ)
        assert stack_env.host_env_problems(rendered, environ) == [], name


def test_both_embedding_examples_render_at_the_vector_columns_width() -> None:
    # ADR-0014: the embedding is the runner's choice; the guide shows OpenAI's and Gemini's.
    examples = [
        _dotenv_values(b)
        for b in _fenced(GUIDE, "dotenv")
        if "EMBEDDING__MODEL_NAME" in b and "EMBEDDING__MOCK" not in b
    ]
    assert sorted(e["EMBEDDING__MODEL_NAME"] for e in examples) == [
        "gemini-embedding-001",
        "text-embedding-3-small",
    ]
    [base] = [b for b in _fenced(GUIDE, "dotenv") if "EMBEDDING__MOCK" in b]
    for example in examples:
        assert set(example) == set(stack_env.EMBEDDING_SETTINGS)
        environ = _with_keys({**_dotenv_values(base), **example})
        rendered = stack_env.render_stack_env(PROFILES["gpt-4o-mini"], environ)
        assert rendered["EMBEDDING__MODEL_NAME"] == example["EMBEDDING__MODEL_NAME"]
        assert rendered["EMBEDDING__DIMENSION"] == "1536"
        assert stack_env.host_env_problems(rendered, environ) == []


def test_the_two_old_lines_the_guide_names_are_the_ones_the_doctor_refuses() -> None:
    for name in doctor.OLD_ENV_LINES:
        assert name in GUIDE


def test_the_guides_use_only_profiles_that_exist() -> None:
    used = set(re.findall(r"(?:MODEL=|--model-profile )([a-z0-9.\-]+)", GUIDE + NATIVE))
    assert used, "the guides name no model profile"
    assert used <= set(PROFILES), used - set(PROFILES)


def test_the_models_are_run_in_the_order_the_runbook_fixes() -> None:
    # ADR-0014: the 2026-10-02 run is full cloud, OpenAI first, then the two OpenRouter models.
    names = ("`gpt-4o-mini`", "`qwen2.5-7b-openrouter`", "`llama-3.1-8b-openrouter`")
    positions = [GUIDE.index(name) for name in names]
    assert positions == sorted(positions)


def test_the_cloud_route_names_its_keys_canary_and_credit_check_before_the_runs() -> None:
    for name in ("BENCH_OPENAI_API_KEY", "BENCH_OPENROUTER_API_KEY", "EMBEDDING__API_KEY"):
        assert name in GUIDE, name
    first_run = GUIDE.index("make bench-run MODEL=qwen2.5-7b-openrouter RUN=2026-10-02")
    assert "make bench-canary MODEL=qwen2.5-7b-openrouter" in GUIDE
    # D4's run list sends the teammate to the credit check and the canary before each run
    assert GUIDE.count("(part E: credit check and canary first)") == 2
    assert "`MODEL=llama-3.1-8b-openrouter` before the Llama run" in GUIDE
    assert "settings/credits" in GUIDE and "settings/privacy" in GUIDE
    # the pins the guide states are the profiles' own
    for profile, provider in (
        ("qwen2.5-7b-openrouter", "Phala"),
        ("llama-3.1-8b-openrouter", "CoreWeave"),
    ):
        assert PROFILES[profile].provider_pin == provider.lower()
        row = next(line for line in GUIDE.splitlines() if line.startswith(f"| `{profile}`"))
        assert provider in row and PROFILES[profile].model in row
    assert GUIDE.index("## E. The OpenRouter route") < GUIDE.index("## L. Appendix: local models")
    assert "not used for the 2026-10-02 run" in GUIDE
    assert first_run < GUIDE.index("## L. Appendix")


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


def test_every_part_of_the_guide_the_doctor_cites_is_a_heading_of_it() -> None:
    source = (REPO_ROOT / "evaluation" / "mailguard_bench" / "kit" / "doctor.py").read_text(
        encoding="utf-8"
    )
    cited = set(re.findall(r"\bpart ([A-Z]\d?)\b", source))
    cited |= set(re.findall(r"\bappendix (L\d?)\b", source))
    headings = set(re.findall(r"^#{2,3} ([A-Z]\d?)\. ", GUIDE, flags=re.MULTILINE))
    assert cited and cited <= headings, cited - headings


def test_the_doctor_sends_local_model_problems_to_appendix_l_not_to_the_openrouter_part() -> None:
    # ADR-0014: part E is the OpenRouter route; Ollama and the GPU moved to appendix L.
    profile = PROFILES["qwen2.5-7b"]
    origin = "http://172.17.0.1:11434"
    down = doctor.check_ollama(
        profile, {"BENCH_OLLAMA_BASE_URL": f"{origin}/v1"}, "wsl2", lambda url: None
    )
    not_pulled = doctor.check_ollama(
        profile,
        {"BENCH_OLLAMA_BASE_URL": f"{origin}/v1"},
        "wsl2",
        {
            f"{origin}/api/version": HttpResult(200, '{"version":"1"}'),
            f"{origin}/api/tags": HttpResult(200, '{"models": []}'),
        }.get,
    )
    gpu = doctor.check_gpu(None, local_model=True)
    assert down is not None and "appendix L4" in down.hint
    assert not_pulled is not None and "appendix L5" in not_pulled.hint
    assert "appendix L1" in gpu.hint
    assert all("part E" not in r.hint for r in (down, not_pulled, gpu))
    assert "### L1. The GPU driver" in GUIDE and "### L5. Pull the models" in GUIDE


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
    # the classifier is the owner's file, not in git (ADR-0012 decision 15): absent on a clone
    missing.discard(pinned.PINNED.classifier.path.as_posix())
    assert missing == set(), missing


def test_the_campaign_commands_of_the_native_guide_parse() -> None:
    campaign = importlib.import_module("evaluation.mailguard_bench.kit.campaign")

    lines = re.findall(
        r"^uv @mg evaluation\.mailguard_bench\.kit\.campaign (.+)$", NATIVE, flags=re.MULTILINE
    )
    assert lines
    for line in lines:
        campaign.parse_args(shlex.split(line.replace("<your reader model>", "reader-x")))


# --- the guide's order, and the commands it names exist -----------------------------------------


def _blocks(text: str) -> list[str]:
    # only blocks with a language (bash, powershell): the overview diagram has none
    return re.findall(r"```[a-z]+\n(.*?)```", text, flags=re.DOTALL)


def _first_block_with(text: str, needles: Sequence[str]) -> int:
    for position, block in enumerate(_blocks(text)):
        if any(needle in block for needle in needles):
            return position
    raise AssertionError(f"no block of the guide holds any of {needles}")


def test_the_guards_fix_is_in_the_guide_before_the_first_doctor_run() -> None:
    def refuse(path: object, commit: str) -> None:
        raise GuardEnvError("worktree not found")

    hint = doctor.check_guard(refuse, REPO_ROOT / "nowhere", "0" * 40).hint
    [command] = re.findall(r"`(make [a-z\-]+)`", hint)
    assert command != "make bench-setup"  # which only checks the worktree
    fix = _first_block_with(GUIDE, [command])
    first_doctor = _first_block_with(GUIDE, ["bench-doctor", "kit.doctor"])
    assert fix < first_doctor, "the teammate would run the doctor before the fix it asks for"
    # The guard is a subtree of the clone (ADR-0012 decision 6): the native guide checks its tree
    # against the pin before the doctor, as `make mailguard-worktree` does in the subtree layout.
    native_fix = _first_block_with(NATIVE, ["git rev-parse HEAD:agentmailguard"])
    assert native_fix < _first_block_with(NATIVE, ["kit.doctor"])
    assert V2_MAILGUARD_TREE in _blocks(NATIVE)[native_fix]


def test_the_guide_places_the_classifier_before_the_first_doctor_run() -> None:
    """D1: the owner sends the file (not in git); it goes in pinned/ and its sha256 is checked."""
    folder = pinned.PINNED.classifier.path.parent.as_posix()
    name = pinned.PINNED.classifier.path.name
    blocks = _blocks(GUIDE)
    place = next(i for i, b in enumerate(blocks) if name in b and folder in b and "cp " in b)
    assert "sha256sum" in blocks[place] or "sha256sum" in blocks[place + 1]
    assert place < _first_block_with(GUIDE, ["bench-doctor", "kit.doctor"])
    assert pinned.PINNED.classifier.sha256 in GUIDE


def test_the_native_guide_points_the_runners_at_the_placed_classifier_before_the_doctor() -> None:
    folder = pinned.PINNED.classifier.path.parent.as_posix()
    want = re.compile(rf"{doctor.ARTIFACTS_ENV}\s*=")
    blocks = _blocks(NATIVE)
    position = next(i for i, b in enumerate(blocks) if want.search(b))
    assert folder.replace("/", "\\") in blocks[position] or folder in blocks[position]
    assert position < _first_block_with(NATIVE, ["kit.doctor"])
    assert pinned.PINNED.classifier.sha256 in NATIVE
    assert any(
        pinned.PINNED.classifier.path.name in b and "Copy-Item" in b for b in _blocks(NATIVE)
    )


def test_both_guides_say_the_classifier_is_not_in_git_and_the_owner_sends_it() -> None:
    for text in (GUIDE, NATIVE):
        assert "not in git" in text, "the guide must say the classifier is not in git"
        assert "ask the owner" in text or "sends you" in text or "sends it" in text
        assert "NOTICE.md" in text
        assert "decision 15" in text
    # the doctor's own fix is the wording the guide uses
    fix = pinned.classifier_fix()
    assert "it is not in git, see NOTICE.md" in fix
    assert pinned.PINNED.classifier.path.name in fix


def test_the_guides_do_not_say_the_classifier_comes_with_the_clone() -> None:
    for text in (GUIDE, NATIVE):
        for line in text.splitlines():
            if pinned.PINNED.classifier.path.name in line:
                assert "come with the clone" not in line and "came with the clone" not in line
                assert "shipped in git" not in line and "committed" not in line


def test_the_primary_route_is_docker_engine_inside_wsl2_and_the_guide_cites_decision_16() -> None:
    assert "decision 16" in GUIDE
    assert "Docker Engine" in GUIDE and "WSL2" in GUIDE
    assert GUIDE.index("A4. Install Docker Engine inside Ubuntu") > GUIDE.index("A1. Install WSL2")
    assert "Docker Desktop" in NATIVE and "Pro, Enterprise or Education" in NATIVE


LOAD_COMMAND = 'ollama run {model} "Reply with OK"'


def test_each_local_run_is_preceded_by_loading_its_model() -> None:
    """The live runner refuses a model that Ollama has not loaded (live/run.py)."""
    for profile_name in ("qwen2.5-7b", "llama-3.1-8b-local"):
        model = PROFILES[profile_name].model
        load = LOAD_COMMAND.format(model=model)
        doctor_hint = doctor.check_model_loaded(
            PROFILES[profile_name], {}, lambda url: HttpResult(200, '{"models": []}')
        )
        assert doctor_hint is not None and doctor_hint.hint == load
        for text, run_marker in (
            (GUIDE, f"make bench-run MODEL={profile_name} "),
            (NATIVE, f"campaign run --model-profile {profile_name} "),
        ):
            block = next(b for b in _blocks(text) if run_marker in b)
            assert load in block, f"{profile_name}: load the model in the same block as its run"
            assert block.index(load) < block.index(run_marker)


def test_the_guide_explains_the_refusal_of_an_unloaded_model() -> None:
    assert "is not loaded" in GUIDE and "refuses" in GUIDE


SOCAT = "socat TCP-LISTEN:11434,bind=<docker0 address>,reuseaddr,fork TCP:127.0.0.1:11434"


def test_the_guide_gives_both_routes_from_the_containers_to_a_loopback_ollama() -> None:
    assert "bridge.conf" in GUIDE and "sudo tee" in GUIDE
    assert SOCAT in GUIDE
    # the doctor's hint names the same forwarder
    hint = doctor.check_ollama(
        PROFILES["qwen2.5-7b"],
        {},
        "wsl2",
        lambda url: (
            HttpResult(200, '{"version":"1","models":[{"name":"qwen2.5:7b-instruct"}]}')
            if "11434/api/" in url and "172." not in url
            else None
        ),
        bridge_ip="172.17.0.1",
    )
    assert hint is not None and SOCAT in hint.hint
    assert "sudo apt install" in GUIDE and "socat" in GUIDE
    assert "bound to the docker0" in GUIDE or "docker0 address only" in GUIDE


# measured on the owner's desktop (RTX 3060 12 GB, 7-case smoke), seconds per case and config
SMOKE_SECONDS_PER_CASE = {"gpt-4o-mini": 3, "qwen2.5-7b": 10, "llama-3.1-8b-local": 9}
STATED_HOURS = {"gpt-4o-mini": (4, 6), "qwen2.5-7b": (12, 15), "llama-3.1-8b-local": (12, 15)}


def test_the_stated_run_times_follow_from_the_smoke_measurements() -> None:
    from evaluation.mailguard_bench.scheme import configs_for

    cases = sum(1 for _ in (REPO_ROOT / pinned.PINNED.cases.path).open(encoding="utf-8"))
    configs = len(configs_for("v2"))
    assert (configs, cases) == (9, 550)
    for profile, seconds in SMOKE_SECONDS_PER_CASE.items():
        hours = configs * cases * seconds / 3600
        low, high = STATED_HOURS[profile]
        assert low <= hours <= high, (profile, hours)
    for text in ("4 to 6 hours", "12 to 15 hours", "estimate", "7-case smoke", "RTX 3060"):
        assert text in GUIDE, text
    assert "about 3 s" in GUIDE and "10 s" in GUIDE and "9 s" in GUIDE


def test_every_make_target_the_guides_name_exists() -> None:
    targets = set(re.findall(r"^([a-z][a-z0-9\-]*)\s*:", MAKEFILE, flags=re.MULTILINE))
    name = r"\bmake ((?:bench|mailguard)-[a-z0-9\-]*[a-z0-9])(?![a-z0-9\-*])"
    named = set(re.findall(name, GUIDE + NATIVE))
    assert named and named <= targets, named - targets


def make_problems(guide: str, makefile: str) -> list[str]:
    """Every `make bench-*` line of a guide needs its target and its variables in the Makefile."""
    targets = set(re.findall(r"^(bench-[a-z]+)\s*:", makefile, flags=re.MULTILINE))
    problems: list[str] = []
    for line in guide.splitlines():
        for target, rest in re.findall(r"make (bench-[a-z]+)((?: [^`\s]+)*)", line):
            if target not in targets:
                problems.append(f"target {target} is not in the Makefile")
            for variable in re.findall(r"\b([A-Z]+)=", rest):
                if f"$({variable})" not in makefile:
                    problems.append(f"variable {variable} of {target} is not in the Makefile")
    return sorted(set(problems))


def test_the_make_target_check_finds_a_missing_target_and_a_missing_variable() -> None:
    run = "make bench-run MODEL=gpt-4o-mini RUN=2026-10-02-x CONCURRENCY=2"
    guide = f"```bash\n{run}\nmake bench-gone\n```\n"
    makefile = "bench-run:\n\t@echo $(MODEL) $(RUN)\n"
    assert make_problems(guide, makefile) == [
        "target bench-gone is not in the Makefile",
        "variable CONCURRENCY of bench-run is not in the Makefile",
    ]
    assert make_problems(guide, makefile + "bench-gone:\n\t@echo $(CONCURRENCY)\n") == []


def test_the_guides_name_make_targets_at_all() -> None:
    names = set(re.findall(r"make (bench-[a-z]+)", GUIDE))
    assert {"bench-doctor", "bench-setup", "bench-run", "bench-report", "bench-package"} <= names


MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


def test_every_make_target_and_variable_the_guides_name_is_in_the_makefile() -> None:
    assert make_problems(GUIDE + NATIVE, MAKEFILE) == []
