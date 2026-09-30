"""The guarded prompt's trusted system instructions carry the reply-format rules (task 7.20).

ADR-0012 2a: the guard's L3 builds its system message from the ``system_instructions`` it is
given, so the rules go there, ahead of the guard's own preamble, from the shared source that the
native templates render from. Untrusted text has no way in: the function takes the profile's
agent instructions and nothing else. The tests that run the real guard pipeline are in
test_mailguard_bench_guarded_reply.py and test_mailguard_live_guarded_drafting.py.
"""

from __future__ import annotations

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
