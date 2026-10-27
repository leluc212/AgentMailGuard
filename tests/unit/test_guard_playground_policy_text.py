"""Guard Playground rule reasons (task 2.3).

Requirements: 5.3 (banner shows the matched rule's reason from configs/policy.yaml),
5.7 ("rule reason not found" when no rule with that id exists).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from services.guard_playground.core.policy_text import REASON_NOT_FOUND, PolicyText
from services.guard_playground.settings import PlaygroundSettings


def _real_policy() -> PolicyText:
    return PolicyText.from_guard_root(PlaygroundSettings().guard_root)


def test_real_policy_every_rule_has_reason() -> None:
    settings = PlaygroundSettings()
    policy = _real_policy()
    assert policy.error is None
    assert policy.version
    raw = yaml.safe_load((settings.guard_root / "configs" / "policy.yaml").read_text("utf-8"))
    assert raw["rules"]
    for rule in raw["rules"]:
        text = policy.reason(rule["id"])
        assert text and text != REASON_NOT_FOUND
        assert text == str(rule["reason"])


def test_real_policy_full_id_and_short_prefix() -> None:
    policy = _real_policy()
    full = policy.reason("P01-critical-injection-quarantine")
    assert full == "Critical-confidence prompt injection or secret exposure."
    assert policy.reason("P01") == full


def test_unknown_and_empty_ids() -> None:
    policy = _real_policy()
    assert policy.reason("P42-does-not-exist") == REASON_NOT_FOUND
    assert policy.reason("P42") == REASON_NOT_FOUND
    assert policy.reason("") == REASON_NOT_FOUND
    assert policy.reason(None) == REASON_NOT_FOUND


def test_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "policy.yaml"
    policy = PolicyText(missing)
    assert policy.error is not None
    assert str(missing) in policy.error
    assert policy.version is None
    assert policy.reason("P01-critical-injection-quarantine") == REASON_NOT_FOUND
    assert policy.reason("P01") == REASON_NOT_FOUND


def test_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text("rules: [unclosed\n", encoding="utf-8")
    policy = PolicyText(path)
    assert policy.error is not None and str(path) in policy.error
    assert policy.reason("P00") == REASON_NOT_FOUND


def test_tmp_yaml_prefix_ambiguity_and_reload_once(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": "test-1",
                "rules": [
                    {"id": "X01-alpha", "reason": "Alpha reason."},
                    {"id": "X02-beta", "reason": "Beta reason."},
                    {"id": "X02-gamma", "reason": "Gamma reason."},
                    {"id": "X03-empty", "reason": ""},
                ],
            }
        ),
        encoding="utf-8",
    )
    policy = PolicyText(path)
    assert policy.error is None
    assert policy.version == "test-1"
    assert policy.reason("X01-alpha") == "Alpha reason."
    assert policy.reason("X01") == "Alpha reason."
    assert policy.reason("X02-gamma") == "Gamma reason."
    assert policy.reason("X02") == REASON_NOT_FOUND  # ambiguous prefix
    assert policy.reason("X03-empty") == REASON_NOT_FOUND
    # Loaded once: later edits to the file do not change lookups.
    path.write_text("rules: []\n", encoding="utf-8")
    assert policy.reason("X01-alpha") == "Alpha reason."
