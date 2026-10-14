"""Guard v3 settings: one section per layer, v2 env keys refused with LegacyConfigError."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from mailguard.config import LEGACY_ENV_KEYS, LegacyConfigError, MailGuardSettings

# A concrete v2 key for each prefix entry of LEGACY_ENV_KEYS (exact entries are used as is).
_PREFIX_SAMPLE = {
    "L1__": "BLOCK_THRESHOLD",
    "L2__": "MAX_BODY_CHARS",
    "L3__": "DATAMARK_CHAR",
    "L3B__": "QUARANTINE_THRESHOLD",
    "L4__": "REDACT_PII",
    "L5__": "AUDIT_LOG_PATH",
}


def _legacy_case(old: str, new: str) -> tuple[str, str]:
    if old.endswith("__"):
        rest = _PREFIX_SAMPLE[old]
        return old + rest, new + rest
    return old, new


LEGACY_CASES = [_legacy_case(old, new) for old, new in LEGACY_ENV_KEYS.items()]


def test_every_prefix_entry_has_a_sample() -> None:
    prefixes = {k for k in LEGACY_ENV_KEYS if k.endswith("__")}
    assert prefixes == set(_PREFIX_SAMPLE)


def test_the_mapping_table_is_the_v3_one() -> None:
    assert LEGACY_ENV_KEYS == {
        "L1__LLM_ENABLED": "INBOUND_GUARD__SCAN_LLM_ENABLED",
        "L1__": "INBOUND_GUARD__",
        "L2__LLM_ENABLED": "INBOUND_GUARD__EXTRACT_LLM_ENABLED",
        "L2__": "INBOUND_GUARD__",
        "L3__": "CHANNEL_ISOLATION__",
        "L3B__": "DOC_SCANNER__",
        "L4__": "OUTPUT_GUARD__",
        "L5__": "POLICY__",
        "GUARD_MODELS__JUDGE": "GUARD_MODELS__INBOUND_JUDGE",
        "GUARD_MODELS__EXTRACTOR": "GUARD_MODELS__INBOUND_EXTRACTOR",
        "GUARD_MODELS__DOC_SCANNER": "GUARD_MODELS__DOC_JUDGE",
    }


@pytest.mark.parametrize(("old", "new"), LEGACY_CASES)
def test_legacy_key_in_the_environment_is_refused(
    monkeypatch: pytest.MonkeyPatch, old: str, new: str
) -> None:
    monkeypatch.setenv(old, "1")
    with pytest.raises(LegacyConfigError) as exc:
        MailGuardSettings(_env_file=None)  # type: ignore[call-arg]
    assert old in str(exc.value)
    assert new in str(exc.value)


@pytest.mark.parametrize(("old", "new"), LEGACY_CASES)
def test_legacy_key_in_the_dotenv_file_is_refused(tmp_path: Path, old: str, new: str) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"{old}=1\n", encoding="utf-8")
    with pytest.raises(LegacyConfigError) as exc:
        MailGuardSettings(_env_file=env_file)  # type: ignore[call-arg]
    assert old in str(exc.value)
    assert new in str(exc.value)


def test_the_default_dotenv_file_is_checked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("L3B__LLM_ENABLED=true\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(LegacyConfigError, match="DOC_SCANNER__LLM_ENABLED"):
        MailGuardSettings()


def test_matching_is_case_insensitive(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("l1__llm_enabled=false\nguard_models__judge=x\n", encoding="utf-8")
    with pytest.raises(LegacyConfigError) as exc:
        MailGuardSettings(_env_file=env_file)  # type: ignore[call-arg]
    assert "INBOUND_GUARD__SCAN_LLM_ENABLED" in str(exc.value)
    assert "GUARD_MODELS__INBOUND_JUDGE" in str(exc.value)


def test_one_error_lists_every_legacy_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("L4__LLM_ENABLED", "true")
    env_file = tmp_path / ".env"
    env_file.write_text("L2__LLM_ENABLED=false\nL5__POLICY_PATH=p.yaml\n", encoding="utf-8")
    with pytest.raises(LegacyConfigError) as exc:
        MailGuardSettings(_env_file=env_file)  # type: ignore[call-arg]
    message = str(exc.value)
    for new in (
        "OUTPUT_GUARD__LLM_ENABLED",
        "INBOUND_GUARD__EXTRACT_LLM_ENABLED",
        "POLICY__POLICY_PATH",
    ):
        assert new in message


def test_legacy_config_error_is_a_value_error() -> None:
    assert issubclass(LegacyConfigError, ValueError)


NEW_KEYS: list[tuple[str, str, str, str, Any]] = [
    ("INBOUND_GUARD__SCAN_LLM_ENABLED", "false", "inbound_guard", "scan_llm_enabled", False),
    ("INBOUND_GUARD__EXTRACT_LLM_ENABLED", "false", "inbound_guard", "extract_llm_enabled", False),
    ("INBOUND_GUARD__ML_MODEL_PATH", "m.joblib", "inbound_guard", "ml_model_path", "m.joblib"),
    ("INBOUND_GUARD__MAX_BODY_CHARS", "500", "inbound_guard", "max_body_chars", 500),
    ("INBOUND_GUARD__STRIP_THRESHOLD", "0.4", "inbound_guard", "strip_threshold", 0.4),
    ("INBOUND_GUARD__BLOCK_THRESHOLD", "0.9", "inbound_guard", "block_threshold", 0.9),
    ("DOC_SCANNER__LLM_ENABLED", "true", "doc_scanner", "llm_enabled", True),
    ("DOC_SCANNER__QUARANTINE_THRESHOLD", "0.6", "doc_scanner", "quarantine_threshold", 0.6),
    (
        "CHANNEL_ISOLATION__SPOTLIGHTING_MODE",
        "delimit",
        "channel_isolation",
        "spotlighting_mode",
        "delimit",
    ),
    ("OUTPUT_GUARD__LLM_ENABLED", "true", "output_guard", "llm_enabled", True),
    ("OUTPUT_GUARD__REDACT_PII", "false", "output_guard", "redact_pii", False),
    ("POLICY__AUDIT_LOG_PATH", "a.jsonl", "policy", "audit_log_path", "a.jsonl"),
    ("GUARD_MODELS__INBOUND_JUDGE", "m1", "guard_models", "inbound_judge", "m1"),
    ("GUARD_MODELS__INBOUND_EXTRACTOR", "m2", "guard_models", "inbound_extractor", "m2"),
    ("GUARD_MODELS__DOC_JUDGE", "m3", "guard_models", "doc_judge", "m3"),
    ("GUARD_MODELS__DOC_SET_JUDGE", "m4", "guard_models", "doc_set_judge", "m4"),
    ("GUARD_MODELS__OUTPUT_JUDGE", "m5", "guard_models", "output_judge", "m5"),
]


@pytest.mark.parametrize(("key", "raw", "section", "field", "value"), NEW_KEYS)
def test_new_key_loads_into_its_field(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    key: str,
    raw: str,
    section: str,
    field: str,
    value: Any,
) -> None:
    monkeypatch.setenv(key, raw)
    from_env = MailGuardSettings(_env_file=None)  # type: ignore[call-arg]
    assert getattr(getattr(from_env, section), field) == value
    monkeypatch.delenv(key)
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key}={raw}\n", encoding="utf-8")
    from_file = MailGuardSettings(_env_file=env_file)  # type: ignore[call-arg]
    assert getattr(getattr(from_file, section), field) == value


def test_no_legacy_key_no_error(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GUARD_MODELS__MODELS_PATH=configs/models.yaml\n", encoding="utf-8")
    s = MailGuardSettings(_env_file=env_file)  # type: ignore[call-arg]
    assert s.guard_models.models_path == "configs/models.yaml"
    assert MailGuardSettings(_env_file=None).policy.policy_path == "configs/policy.yaml"  # type: ignore[call-arg]


def test_defaults() -> None:
    s = MailGuardSettings(_env_file=None)  # type: ignore[call-arg]
    gm = s.guard_models
    assert (
        gm.inbound_judge,
        gm.inbound_extractor,
        gm.doc_judge,
        gm.doc_set_judge,
        gm.output_judge,
    ) == ("fake",) * 5
    assert s.inbound_guard.ml_model_path == "artifacts/models/l1_injection_clf_v1.joblib"
    assert s.inbound_guard.scan_llm_enabled and s.inbound_guard.extract_llm_enabled
    assert not s.doc_scanner.llm_enabled and not s.output_guard.llm_enabled


def test_flag_threshold_must_not_exceed_block_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INBOUND_GUARD__FLAG_THRESHOLD", "0.9")
    monkeypatch.setenv("INBOUND_GUARD__BLOCK_THRESHOLD", "0.5")
    with pytest.raises(ValidationError, match="flag_threshold must be <= block_threshold"):
        MailGuardSettings(_env_file=None)  # type: ignore[call-arg]
