"""The reply-format rules have one source that both prompts render from (task 7.20, ADR-0012 2a).

rag-email's own prompt (C0) tells the model how to answer: cite the knowledge chunks it used and
conform to the JSON schema. The guarded prompt (C0T, C1, C2, C3) did not, and Llama answered with a
greeting only. The rules now live in ``packages/llm/reply_format.py``; the native templates render
them from there and the guarded trusted system instructions carry them (evaluation/mailguard_bench/
guarded_reply.py). The native prompt itself is pinned by test_native_prompt_golden.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.llm import reply_format
from packages.llm.profile import AgentProfileRegistry
from tests.unit.test_native_prompt_golden import PROFILES, REGISTRY_PATH, package

TEMPLATES = sorted(Path("prompts").glob("*.v[23].j2"))


def test_the_shared_rules_are_the_two_the_native_templates_always_had() -> None:
    assert reply_format.REPLY_FORMAT_RULES == (
        "Every cited chunk must be included in `knowledge_chunks` with its exact citation ID.",
        "Output must strictly conform to the required JSON schema.",
    )


def test_numbered_rules_continue_the_callers_numbering() -> None:
    assert reply_format.render_reply_format_rules(start=3) == (
        "3. Every cited chunk must be included in `knowledge_chunks` with its exact citation ID."
        "\n4. Output must strictly conform to the required JSON schema."
    )


def test_there_is_a_native_template_per_profile_and_version() -> None:
    assert len(TEMPLATES) == 8  # four profiles, prompt versions v2 and v3


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_native_template_keeps_its_own_copy_of_the_rules(template: Path) -> None:
    source = template.read_text(encoding="utf-8")
    for rule in reply_format.REPLY_FORMAT_RULES:
        assert rule not in source
    assert "reply_format_rules(3)" in source


@pytest.mark.parametrize("profile", PROFILES)
def test_the_native_prompt_renders_the_rules_from_the_shared_source(
    profile: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reply_format, "REPLY_FORMAT_RULES", ("Sentinel one.", "Sentinel two."))
    registry = AgentProfileRegistry.from_yaml(REGISTRY_PATH)

    rendered = registry.render_prompt(profile, package(full=True))

    assert rendered.endswith("\n3. Sentinel one.\n4. Sentinel two.")
