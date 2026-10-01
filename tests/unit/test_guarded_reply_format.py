"""The guarded prompt's trusted system instructions carry the reply-format rules (task 7.20).

ADR-0012 2a: the guard's L3 builds its system message from the ``system_instructions`` it is
given, so the rules go there, ahead of the guard's own preamble, from the shared source that the
native templates render from. Untrusted text has no way in: the function takes the profile's
agent instructions and nothing else. The tests that run the real guard pipeline are in
test_mailguard_bench_guarded_reply.py and test_mailguard_live_guarded_drafting.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.mailguard_bench.guarded_reply import (
    GUARDED_PROMPT_VERSION,
    guarded_system_instructions,
)
from packages.llm import reply_format

AGENT = "You are an enterprise support assistant."


def test_the_guarded_prompt_has_its_own_recorded_version() -> None:
    assert GUARDED_PROMPT_VERSION == "guarded.v2"


def test_the_agent_instructions_come_first_then_every_shared_rule() -> None:
    text = guarded_system_instructions(AGENT)

    assert text.startswith(AGENT + "\n\n")
    for rule in reply_format.REPLY_FORMAT_RULES:
        assert text.count(rule) == 1
    assert text.index(AGENT) < min(text.index(r) for r in reply_format.REPLY_FORMAT_RULES)


def test_the_rules_are_read_from_the_shared_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reply_format, "REPLY_FORMAT_RULES", ("Sentinel one.", "Sentinel two."))

    text = guarded_system_instructions(AGENT)

    assert text.endswith("1. Sentinel one.\n2. Sentinel two.")
    assert "Every cited chunk" not in text


@pytest.mark.parametrize("agent", [None, "", "   \n"])
def test_a_profile_without_agent_instructions_still_gets_the_rules(agent: str | None) -> None:
    text = guarded_system_instructions(agent)

    assert text.startswith("Reply format rules:")
    for rule in reply_format.REPLY_FORMAT_RULES:
        assert rule in text


def test_the_function_takes_the_agent_instructions_only() -> None:
    # Nothing else can reach the trusted section: no email, thread or chunk parameter exists.
    import inspect

    assert list(inspect.signature(guarded_system_instructions).parameters) == ["agent_instructions"]


# --- the version is part of every run's meta and settings fingerprint, so runs never mix


def test_the_version_is_a_fingerprint_key_of_every_runner_and_shared_by_the_configs() -> None:
    from evaluation.mailguard_bench.live import guard_worker
    from evaluation.mailguard_bench.report import SHARED_SETTINGS
    from evaluation.mailguard_bench.runner import FINGERPRINT_KEYS

    assert "guarded_prompt_version" in FINGERPRINT_KEYS  # the v1 runner and the live runner
    assert "guarded_prompt_version" in guard_worker.FINGERPRINT_KEYS
    assert "guarded_prompt_version" in SHARED_SETTINGS  # the report refuses a mix in one run


def test_a_run_started_before_the_prompt_version_existed_cannot_be_resumed(
    tmp_path: Path,
) -> None:
    from evaluation.mailguard_bench.runner import (
        RunSettingsMismatchError,
        check_resume,
        settings_fingerprint,
    )

    v1_meta = {"preset": "C3", "mailguard_commit": "8" * 40}
    v2_meta = {**v1_meta, "guarded_prompt_version": GUARDED_PROMPT_VERSION}
    meta_file = tmp_path / "C3.meta.json"
    meta_file.write_text(
        json.dumps({"fingerprint": settings_fingerprint(v1_meta), "invocations": []}), "utf-8"
    )

    with pytest.raises(RunSettingsMismatchError, match="guarded_prompt_version"):
        check_resume(meta_file, settings_fingerprint(v2_meta))
