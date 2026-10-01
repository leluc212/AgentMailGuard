"""Reply prompts say they draft the reply email to the customer (task 7.27, ADR-0012 decision 14).

Found in the live smoke of 2026-09-30: Llama-3.1-8B answered rag-email's general reply prompt, whose
task line asked for "a polite, helpful, and concise response", with "Your email draft is ready.".
The support prompt, which says "draft response to the customer", got a real reply.

THE RULE (one rule for every reply template, decided once)
    The task line of a reply template, the first numbered line under ``Instructions:``, must begin
    with "Draft" and name its output as "the reply email to the customer". A line that calls its
    output a "response" or a "reply" without saying it is the email to the customer does not pass.

Every template that fails the rule gets the wording as a NEW file and prompt version; the old
versions stay byte for byte (the v1 runs recorded them), and nothing else in a template changes.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path

import pytest
import yaml

from evaluation.mailguard_bench.guarded_reply import (
    GUARDED_PROMPT_VERSION,
    guarded_system_instructions,
)
from packages.llm.profile import DEFAULT_ENTERPRISE_INSTRUCTIONS, AgentProfileRegistry
from tests.unit.test_native_prompt_golden import PROFILES, REGISTRY_PATH, package

GOLDEN_V3 = Path(__file__).parent / "golden" / "native_prompt_v3"
STEMS = {
    "technical_support": "support",
    "billing": "billing",
    "sales": "sales",
    "general_inquiry": "general",
}
REQUIRED_PHRASE = "reply email to the customer"

# The templates the v1 runs recorded. They stay untouched: a change here changes what those runs
# say they sent, so it is a new version file instead.
OLD_TEMPLATE_SHA256 = {
    "billing.v1.j2": "70227056a0608f51aa10a8cfb83d00fd0baed72d6777fcb7c8e5aee1ba20fd88",
    "billing.v2.j2": "2de22740a9fd4890661ede38a9d5b4d9e24f62a480da4ef33e8291da7e5236a1",
    "general.v1.j2": "ed5a721c33e6851b1c2067d67cb5f27e49478029465cd80e179ab3713d28eeac",
    "general.v2.j2": "90f28d673ad5362f89da7f42ac2eb24401ddf192bc7b482667b31f18200e223f",
    "sales.v1.j2": "3283cf6f17ec53afb9f778a53a345f2dd1577844b4270bcfa93832a98ab3c0b9",
    "sales.v2.j2": "9510ee48d5a4555d0d48bb2c00407a9eb8cc60e601712dbbca69170cfc70e22a",
    "support.v1.j2": "99ad76a3b10d61dbde58ce8bacabc1a061bc61898d256b28cb63936ea40931a3",
    "support.v2.j2": "7969daec66c20b86f1d791fa09f6df7d3a67066220849c5b35fb7cb704249eeb",
}


def task_line(template_source: str) -> str:
    """The first numbered line under ``Instructions:`` of a reply template."""
    _, _, after = template_source.partition("Instructions:\n")
    match = re.search(r"^1\. (.+)$", after, flags=re.MULTILINE)
    assert match, "the template has no numbered task line under 'Instructions:'"
    return match.group(1)


def _profiles() -> dict[str, dict[str, str]]:
    data = yaml.safe_load(Path(REGISTRY_PATH).read_text(encoding="utf-8"))
    profiles: dict[str, dict[str, str]] = data["profiles"]
    return profiles


ACTIVE = sorted(_profiles().items())


@pytest.mark.parametrize(("name", "profile"), ACTIVE, ids=[n for n, _ in ACTIVE])
def test_every_active_reply_template_says_it_drafts_the_reply_to_the_customer(
    name: str, profile: dict[str, str]
) -> None:
    line = task_line(Path(profile["prompt_template"]).read_text(encoding="utf-8"))

    assert line.startswith("Draft "), f"{name}: {line!r}"
    assert REQUIRED_PHRASE in line, f"{name}: {line!r}"


@pytest.mark.parametrize(("name", "profile"), ACTIVE, ids=[n for n, _ in ACTIVE])
def test_every_profile_names_an_existing_template_whose_file_name_is_its_prompt_version(
    name: str, profile: dict[str, str]
) -> None:
    template = Path(profile["prompt_template"])

    assert template.is_file(), f"{name}: {template} does not exist"
    assert template.parent == Path("prompts")
    assert template.suffix == ".j2"
    assert profile["prompt_version"] == template.name.removesuffix(".j2"), name
    assert profile["prompt_version"].startswith(f"{STEMS[name]}.v")


def test_every_reply_profile_is_covered() -> None:
    assert {name for name, _ in ACTIVE} == set(STEMS)


@pytest.mark.parametrize("old", sorted(OLD_TEMPLATE_SHA256))
def test_old_template_versions_are_byte_for_byte_what_the_earlier_runs_recorded(old: str) -> None:
    digest = hashlib.sha256(Path("prompts", old).read_bytes()).hexdigest()

    assert digest == OLD_TEMPLATE_SHA256[old]


@pytest.mark.parametrize("profile", PROFILES)
def test_the_v3_prompt_differs_from_v2_in_the_task_line_only(profile: str) -> None:
    registry = AgentProfileRegistry.from_yaml(REGISTRY_PATH)
    active = registry.get_profile(profile)
    assert active is not None
    stem = STEMS[profile]
    assert active.prompt_template == f"prompts/{stem}.v3.j2"
    v2 = active.model_copy(update={"prompt_template": f"prompts/{stem}.v2.j2"})

    for full in (True, False):
        old = registry.render_prompt(v2, package(full=full)).splitlines()
        new = registry.render_prompt(active, package(full=full)).splitlines()
        changed = [
            d for d in difflib.ndiff(old, new) if d.startswith(("+ ", "- ")) and d[2:].strip()
        ]
        assert len(changed) == 2, changed  # one line out, one line in
        assert changed[0].startswith("- 1. ") and changed[1].startswith("+ 1. "), changed
        assert REQUIRED_PHRASE in changed[1]


@pytest.mark.parametrize("full", [True, False], ids=["full_context", "email_only"])
@pytest.mark.parametrize("profile", PROFILES)
def test_the_v3_native_prompt_is_pinned_next_to_the_v2_golden(profile: str, full: bool) -> None:
    registry = AgentProfileRegistry.from_yaml(REGISTRY_PATH)
    rendered = registry.render_prompt(profile, package(full=full))
    golden = (GOLDEN_V3 / f"{profile}.{'full' if full else 'email_only'}.txt").read_bytes()

    assert rendered.encode("utf-8") == golden


def test_the_guarded_prompt_does_not_carry_a_template_task_line() -> None:
    """Why ``guarded.v2`` does not move: the guarded prompt never includes the template's text.

    The guarded system instructions are the profile's agent instructions plus the shared reply
    format rules, and the task line of the guarded prompt is AgentMailGuard's own ``[TASK]`` line
    ("draft a reply to the customer's request"). If a task line of any template version ever
    reached the guarded prompt, this fails and the guarded version must move.
    """
    instructions = guarded_system_instructions(DEFAULT_ENTERPRISE_INSTRUCTIONS)
    lines = [
        task_line(path.read_text(encoding="utf-8"))
        for path in sorted(Path("prompts").glob("*.v[23].j2"))
    ]

    assert len(lines) == 8
    for line in lines:
        assert line not in instructions
    assert "Draft the reply email" not in instructions
    assert GUARDED_PROMPT_VERSION == "guarded.v2"
