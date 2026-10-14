"""Validated configuration for every guard layer (pydantic-settings, ``__`` nesting)."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# v2 environment keys and their v3 replacements (ADR-0015 decision 6: hard rename, no aliases).
# A key ending in "__" is a prefix: the rest of the key is kept (L4__REDACT_PII ->
# OUTPUT_GUARD__REDACT_PII). Exact keys are listed before the prefix they would also match.
LEGACY_ENV_KEYS: dict[str, str] = {
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


class LegacyConfigError(ValueError):
    """A v2 guard environment key is set; the message names its v3 replacement."""


def legacy_replacement(key: str) -> str | None:
    """The v3 key for a v2 guard key (case-insensitive), None if ``key`` is not a v2 key."""
    upper = key.upper()
    if upper in LEGACY_ENV_KEYS and not upper.endswith("__"):
        return LEGACY_ENV_KEYS[upper]
    for old, new in LEGACY_ENV_KEYS.items():
        if old.endswith("__") and upper.startswith(old) and len(upper) > len(old):
            return new + upper[len(old) :]
    return None


def _check_legacy_keys(sources: Iterable[tuple[str, Iterable[str]]]) -> None:
    found: list[str] = []
    for origin, keys in sources:
        for key in keys:
            new = legacy_replacement(key)
            if new is not None:
                found.append(f"  {key} ({origin}) -> {new}")
    if found:
        raise LegacyConfigError(
            "v2 guard config keys are no longer read (guard v3, ADR-0015); rename them:\n"
            + "\n".join(found)
        )


class OllamaSettings(BaseModel):
    base_url: str = Field(default="http://localhost:11434")
    timeout_s: float = Field(default=60.0, ge=1.0)


class GuardModelSettings(BaseModel):
    """Which registered model (configs/models.yaml) backs each LLM-based stage."""

    inbound_judge: str = Field(default="fake", description="Inbound Guard scan LLM judge")
    inbound_extractor: str = Field(default="fake", description="Inbound Guard intent extractor")
    doc_judge: str = Field(default="fake", description="Doc Scanner per-chunk LLM judge")
    doc_set_judge: str = Field(default="fake", description="Doc Scanner set judge (Task 10)")
    output_judge: str = Field(default="fake", description="Output Guard LLM judge")
    models_path: str = Field(default="configs/models.yaml")


class InboundGuardSettings(BaseModel):
    """L1 Inbound Guard: the scan (rules, ML, judge) and the extract sub-stage."""

    rules_path: str = Field(default="configs/injection_rules.yaml")
    ml_model_path: str = Field(default="artifacts/models/l1_injection_clf_v1.joblib")
    rule_confidence_threshold: float = Field(default=0.90, ge=0.0, le=1.0)
    ml_confidence_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    scan_llm_enabled: bool = Field(default=True)
    block_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    flag_threshold: float = Field(default=0.50, ge=0.0, le=1.0)
    max_chars: int = Field(default=12000, ge=200)
    extract_llm_enabled: bool = Field(default=True)
    max_body_chars: int = Field(default=6000, ge=200)
    strip_threshold: float = Field(
        default=0.50, ge=0.0, le=1.0, description="Segment score >= threshold is stripped"
    )

    @model_validator(mode="after")
    def _order(self) -> InboundGuardSettings:
        if self.flag_threshold > self.block_threshold:
            raise ValueError("flag_threshold must be <= block_threshold")
        return self


class ChannelIsolationSettings(BaseModel):
    spotlighting_mode: str = Field(default="datamark", description="delimit | datamark | encode")
    datamark_char: str = Field(default="^", min_length=1, max_length=1)
    channels_path: str = Field(default="configs/channels.yaml")
    max_untrusted_tokens: int = Field(default=6000, ge=100)

    @model_validator(mode="after")
    def _mode(self) -> ChannelIsolationSettings:
        if self.spotlighting_mode not in {"delimit", "datamark", "encode"}:
            raise ValueError("spotlighting_mode must be delimit | datamark | encode")
        return self


class DocScannerSettings(BaseModel):
    quarantine_threshold: float = Field(default=0.70, ge=0.0, le=1.0)
    llm_enabled: bool = Field(default=False)
    max_quarantine_ratio: float = Field(
        default=0.60, ge=0.0, le=1.0, description="If more chunks are quarantined, escalate"
    )


class OutputGuardSettings(BaseModel):
    pii_patterns_path: str = Field(default="configs/pii_patterns.yaml")
    redact_pii: bool = Field(default=True)
    system_prompt_leak_ngram: int = Field(default=8, ge=3)
    llm_enabled: bool = Field(default=False)
    external_domain_allowlist: list[str] = Field(default_factory=list)


class PolicySettings(BaseModel):
    policy_path: str = Field(default="configs/policy.yaml")
    audit_log_path: str = Field(default="logs/mailguard_audit.jsonl")


class TelemetrySettings(BaseModel):
    prometheus_enabled: bool = Field(default=False)
    metrics_port: int = Field(default=9101, ge=1, le=65535)
    log_level: str = Field(default="INFO")


class MailGuardCoreSettings(BaseModel):
    enabled: bool = Field(default=True)
    fail_closed: bool = Field(
        default=True, description="Internal error is treated as HIGH severity (never fail open)"
    )


def _dotenv_keys(env_file: Any, encoding: str | None) -> list[tuple[str, list[str]]]:
    """Keys of the dotenv file(s) pydantic-settings will read (missing files are skipped)."""
    if env_file is None:
        return []
    files = [env_file] if isinstance(env_file, str | os.PathLike) else list(env_file)
    out: list[tuple[str, list[str]]] = []
    for f in files:
        path = Path(f).expanduser()
        if path.is_file():
            values: Mapping[str, str | None] = dotenv_values(path, encoding=encoding)
            out.append((str(path), list(values)))
    return out


class MailGuardSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_nested_delimiter="__", extra="ignore"
    )

    environment: str = Field(default="development")
    mailguard: MailGuardCoreSettings = Field(default_factory=MailGuardCoreSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    guard_models: GuardModelSettings = Field(default_factory=GuardModelSettings)
    inbound_guard: InboundGuardSettings = Field(default_factory=InboundGuardSettings)
    doc_scanner: DocScannerSettings = Field(default_factory=DocScannerSettings)
    channel_isolation: ChannelIsolationSettings = Field(default_factory=ChannelIsolationSettings)
    output_guard: OutputGuardSettings = Field(default_factory=OutputGuardSettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)

    def __init__(self, **values: Any) -> None:
        # v2 keys would be silently ignored (extra="ignore"); refuse them before loading.
        config = type(self).model_config
        env_file = values["_env_file"] if "_env_file" in values else config.get("env_file")
        encoding = values.get("_env_file_encoding") or config.get("env_file_encoding")
        _check_legacy_keys([("environment", list(os.environ)), *_dotenv_keys(env_file, encoding)])
        super().__init__(**values)

    def resolve(self, relative: str) -> Path:
        """Resolve a config-relative path against the project root."""
        p = Path(relative)
        return p if p.is_absolute() else PROJECT_ROOT / p


@lru_cache(maxsize=1)
def get_settings() -> MailGuardSettings:
    return MailGuardSettings()
