"""Validated configuration for every guard layer (pydantic-settings, ``__`` nesting)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class OllamaSettings(BaseModel):
    base_url: str = Field(default="http://localhost:11434")
    timeout_s: float = Field(default=60.0, ge=1.0)


class GuardModelSettings(BaseModel):
    """Which registered model (configs/models.yaml) backs each LLM-based stage."""

    judge: str = Field(default="fake", description="L1 stage-3 LLM judge")
    extractor: str = Field(default="fake", description="L2 intent extractor")
    doc_scanner: str = Field(default="fake", description="L3b LLM scanner")
    output_judge: str = Field(default="fake", description="L4 LLM judge")
    models_path: str = Field(default="configs/models.yaml")


class L1Settings(BaseModel):
    rules_path: str = Field(default="configs/injection_rules.yaml")
    ml_model_path: str = Field(default="artifacts/models/l1_injection_clf_v1.joblib")
    rule_confidence_threshold: float = Field(default=0.90, ge=0.0, le=1.0)
    ml_confidence_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    llm_enabled: bool = Field(default=True)
    block_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    flag_threshold: float = Field(default=0.50, ge=0.0, le=1.0)
    max_chars: int = Field(default=12000, ge=200)

    @model_validator(mode="after")
    def _order(self) -> L1Settings:
        if self.flag_threshold > self.block_threshold:
            raise ValueError("flag_threshold must be <= block_threshold")
        return self


class L2Settings(BaseModel):
    llm_enabled: bool = Field(default=True)
    max_body_chars: int = Field(default=6000, ge=200)
    strip_threshold: float = Field(
        default=0.50, ge=0.0, le=1.0, description="Segment score >= threshold is stripped"
    )


class L3Settings(BaseModel):
    spotlighting_mode: str = Field(default="datamark", description="delimit | datamark | encode")
    datamark_char: str = Field(default="^", min_length=1, max_length=1)
    channels_path: str = Field(default="configs/channels.yaml")
    max_untrusted_tokens: int = Field(default=6000, ge=100)

    @model_validator(mode="after")
    def _mode(self) -> L3Settings:
        if self.spotlighting_mode not in {"delimit", "datamark", "encode"}:
            raise ValueError("spotlighting_mode must be delimit | datamark | encode")
        return self


class L3bSettings(BaseModel):
    quarantine_threshold: float = Field(default=0.70, ge=0.0, le=1.0)
    llm_enabled: bool = Field(default=False)
    max_quarantine_ratio: float = Field(
        default=0.60, ge=0.0, le=1.0, description="If more chunks are quarantined, escalate"
    )


class L4Settings(BaseModel):
    pii_patterns_path: str = Field(default="configs/pii_patterns.yaml")
    redact_pii: bool = Field(default=True)
    system_prompt_leak_ngram: int = Field(default=8, ge=3)
    llm_enabled: bool = Field(default=False)
    external_domain_allowlist: list[str] = Field(default_factory=list)


class L5Settings(BaseModel):
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


class MailGuardSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_nested_delimiter="__", extra="ignore"
    )

    environment: str = Field(default="development")
    mailguard: MailGuardCoreSettings = Field(default_factory=MailGuardCoreSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    guard_models: GuardModelSettings = Field(default_factory=GuardModelSettings)
    l1: L1Settings = Field(default_factory=L1Settings)
    l2: L2Settings = Field(default_factory=L2Settings)
    l3: L3Settings = Field(default_factory=L3Settings)
    l3b: L3bSettings = Field(default_factory=L3bSettings)
    l4: L4Settings = Field(default_factory=L4Settings)
    l5: L5Settings = Field(default_factory=L5Settings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)

    def resolve(self, relative: str) -> Path:
        """Resolve a config-relative path against the project root."""
        p = Path(relative)
        return p if p.is_absolute() else PROJECT_ROOT / p


@lru_cache(maxsize=1)
def get_settings() -> MailGuardSettings:
    return MailGuardSettings()
