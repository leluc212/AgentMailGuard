"""Unit tests for configuration and settings management (R20.6, R5.10, R21.6)."""

import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from packages.core.settings import (
    AIWorkerSettings,
    APISettings,
    AppSettings,
    BusinessDataSettings,
    DatabaseSettings,
    DispatchWorkerSettings,
    EmailWorkerSettings,
    FrontendServiceSettings,
    FrontendSettings,
    KnowledgeWorkerSettings,
    LLMTiersSettings,
    MailConnectorSettings,
    ModelPricing,
    RetryLadderSettings,
    TriageSettings,
    TriageWorkerSettings,
    assert_embedding_dimension,
)


def test_default_app_settings_instantiation() -> None:
    """Verify default instantiation contains all 11 architectural groups with expected defaults."""
    settings = AppSettings(_env_file=None)

    # 1. Database
    assert settings.database.port == 5432
    assert "rag_email" in settings.database.dsn

    # 2. Broker
    assert settings.broker.port == 5672
    assert "amqp://" in settings.broker.url
    assert settings.broker.exchange_email_route == "email.route"
    assert settings.broker.queue_dead_letter == "email.dead_letter"
    assert settings.broker.use_quorum_queues is False

    # 3. Object Storage
    assert settings.object_storage.endpoint == "localhost:9000"
    assert settings.object_storage.bucket_raw_mime == "raw-mime"

    # 4. Provider Credentials
    assert settings.providers.mock_providers is True
    assert settings.providers.gmail_credentials_ref == "vault/gmail"

    # 5. Embedding
    assert settings.embedding.dimension == 1536
    assert settings.embedding.model_name == "text-embedding-3-small"
    assert settings.embedding.mock is True
    assert settings.embedding.base_url == "https://api.openai.com/v1"
    assert settings.embedding.api_key is None
    assert settings.embedding.timeout_s == 10.0
    assert settings.embedding.max_retries == 3
    assert settings.embedding.retry_delay_s == 0.5

    # 6. LLM Tiers & Price Table
    assert settings.llm.fast_model == "gpt-4o-mini"
    assert "gpt-4o" in settings.llm.price_table
    assert settings.llm.price_table["gpt-4o"].input_per_m == 5.00

    # 7. Retrieval
    assert settings.retrieval.top_n == 20
    assert settings.retrieval.rrf_k == 60
    assert settings.retrieval.top_k == 5
    assert settings.retrieval.rerank_enabled is True

    # 8. Triage
    assert settings.triage.rule_confidence_threshold == 0.95
    assert settings.triage.safe_default_category == "general_inquiry"

    # 9. Summarization
    assert settings.summarization.min_messages_threshold == 4
    assert settings.summarization.context_token_threshold == 1500

    # 10. Retry Ladder
    assert settings.retry.tier_1_delay_s == 30
    assert settings.retry.tier_2_delay_s == 300
    assert settings.retry.tier_3_delay_s == 1800

    # 11. Worker Concurrency
    assert settings.concurrency.default_prefetch == 10
    assert settings.concurrency.ai_worker_concurrency == 4


def test_specialized_service_settings() -> None:
    """Verify service-specific settings classes specify proper service_name."""
    assert APISettings(_env_file=None).service_name == "api"
    assert MailConnectorSettings(_env_file=None).service_name == "mail_connector"
    assert EmailWorkerSettings(_env_file=None).service_name == "email_worker"
    assert TriageWorkerSettings(_env_file=None).service_name == "triage_worker"
    assert AIWorkerSettings(_env_file=None).service_name == "ai_worker"
    assert KnowledgeWorkerSettings(_env_file=None).service_name == "knowledge_worker"
    assert DispatchWorkerSettings(_env_file=None).service_name == "dispatch_worker"


def test_environment_variable_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify environment variables correctly override nested settings."""
    monkeypatch.setenv("DATABASE__PORT", "5433")
    monkeypatch.setenv("EMBEDDING__DIMENSION", "768")
    monkeypatch.setenv("RETRIEVAL__TOP_K", "8")
    monkeypatch.setenv("LLM__FORCE_SINGLE_TIER", "true")

    settings = AppSettings(_env_file=None)
    assert settings.database.port == 5433
    assert settings.embedding.dimension == 768
    assert settings.retrieval.top_k == 8
    assert settings.llm.force_single_tier is True


def test_fail_fast_on_invalid_port() -> None:
    """Verify fail-fast validation on illegal database port."""
    with pytest.raises(ValidationError):
        DatabaseSettings(port=99999)


def test_fail_fast_on_invalid_confidence_threshold() -> None:
    """Verify fail-fast validation on illegal confidence values (> 1.0)."""
    with pytest.raises(ValidationError):
        TriageSettings(rule_confidence_threshold=1.5)


def test_fail_fast_on_invalid_triage_threshold_order() -> None:
    """Verify rule threshold must be >= ml threshold."""
    with pytest.raises(
        ValidationError, match="rule_confidence_threshold must be >= ml_confidence_threshold"
    ):
        TriageSettings(rule_confidence_threshold=0.5, ml_confidence_threshold=0.8)


def test_fail_fast_on_invalid_retry_ladder_progression() -> None:
    """Verify retry intervals must be strictly ascending."""
    with pytest.raises(ValidationError, match="strictly ascending"):
        RetryLadderSettings(tier_1_delay_s=300, tier_2_delay_s=30, tier_3_delay_s=1800)


def test_fail_fast_on_negative_pricing() -> None:
    """Verify negative token pricing is disallowed."""
    with pytest.raises(ValidationError):
        ModelPricing(input_per_m=-0.5, output_per_m=1.0)


def test_assert_embedding_dimension() -> None:
    """Verify startup assertion for vector dimensionality (R5.10)."""
    # Success path
    assert_embedding_dimension(configured_dim=1536, db_column_dim=1536)

    # Failure path
    with pytest.raises(ValueError, match="does not match database column width"):
        assert_embedding_dimension(configured_dim=1536, db_column_dim=768)


def test_worker_micro_batch_settings_validation() -> None:
    """Verify micro_batch_size and micro_batch_timeout_ms bounds (R3.6)."""
    from packages.core.settings import WorkerConcurrencySettings

    cfg = WorkerConcurrencySettings(
        default_prefetch=10,
        micro_batch_size=5,
        micro_batch_timeout_ms=100,
    )
    assert cfg.micro_batch_size == 5
    assert cfg.micro_batch_timeout_ms == 100

    # micro_batch_size cannot exceed prefetch
    with pytest.raises(ValidationError, match="cannot exceed default_prefetch"):
        WorkerConcurrencySettings(default_prefetch=4, micro_batch_size=8)


def test_retry_ladder_backoff_settings_validation() -> None:
    """Verify backoff factor, bounds, and jitter mode validation (R19.5)."""
    cfg = RetryLadderSettings(
        tier_1_delay_s=30,
        tier_2_delay_s=300,
        tier_3_delay_s=1800,
        backoff_base_s=2.0,
        backoff_factor=3.0,
        max_backoff_s=1200.0,
        jitter_mode="equal",
    )
    assert cfg.backoff_base_s == 2.0
    assert cfg.backoff_factor == 3.0
    assert cfg.max_backoff_s == 1200.0
    assert cfg.jitter_mode == "equal"

    # Invalid jitter_mode raises ValidationError
    with pytest.raises(ValidationError, match="Invalid jitter_mode"):
        RetryLadderSettings(jitter_mode="unknown_jitter")


GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_PRICE_TABLE = {
    "gemma-4-26b-a4b-it": {"input_per_m": 0, "output_per_m": 0},
    "gemma-4-31b-it": {"input_per_m": 0, "output_per_m": 0},
    "gemini-3.1-flash-lite": {"input_per_m": 0.25, "output_per_m": 1.50},
}


@pytest.mark.parametrize("field", ["fast_model", "strong_model", "fallback_model"])
@pytest.mark.parametrize(
    "model", ["gemma-4-26b-a4b-it", "gemini-3.1-flash-lite", "Gemini-3.1-Flash-Lite"]
)
def test_openai_provider_refuses_google_models_on_the_openai_url(field: str, model: str) -> None:
    """R20.6: a Gemini/Gemma model on the OpenAI default URL fails fast; the key stays home."""
    with pytest.raises(ValidationError, match="LLM__OPENAI_BASE_URL is the OpenAI default"):
        LLMTiersSettings.model_validate({"provider": "openai", "openai_api_key": "k", field: model})


def test_openai_default_url_with_trailing_slash_is_still_refused() -> None:
    with pytest.raises(ValidationError, match="LLM__OPENAI_BASE_URL is the OpenAI default"):
        LLMTiersSettings(
            provider="openai",
            openai_base_url="https://api.openai.com/v1/",
            fast_model="gemma-4-26b-a4b-it",
        )


def test_gemini_models_on_the_gemini_url_are_accepted() -> None:
    llm = LLMTiersSettings(
        provider="openai",
        openai_base_url=GEMINI_BASE_URL,
        fast_model="gemma-4-26b-a4b-it",
        strong_model="gemma-4-31b-it",
        fallback_model="gemini-3.1-flash-lite",
    )
    assert llm.openai_base_url == GEMINI_BASE_URL
    assert llm.fallback_model == "gemini-3.1-flash-lite"


def test_openai_models_on_the_openai_url_are_accepted() -> None:
    llm = LLMTiersSettings(provider="openai", fast_model="gpt-4o-mini", strong_model="gpt-4o")
    assert llm.openai_base_url == "https://api.openai.com/v1"


def test_fake_provider_ignores_google_model_names() -> None:
    """The fake provider sends nothing, so the endpoint check does not apply."""
    llm = LLMTiersSettings(provider="fake", fast_model="gemma-4-26b-a4b-it")
    assert llm.fast_model == "gemma-4-26b-a4b-it"


def test_blank_llm_env_values_keep_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose forwards an unset host variable as ''; that must mean 'use the default'."""
    for var in (
        "LLM__OPENAI_BASE_URL",
        "LLM__FAST_MODEL",
        "LLM__STRONG_MODEL",
        "LLM__FALLBACK_MODEL",
        "LLM__PRICE_TABLE",
    ):
        monkeypatch.setenv(var, "")
    llm = AppSettings(_env_file=None).llm
    defaults = LLMTiersSettings()
    assert llm.openai_base_url == defaults.openai_base_url
    assert llm.fast_model == defaults.fast_model
    assert llm.strong_model == defaults.strong_model
    assert llm.fallback_model == defaults.fallback_model
    assert llm.price_table == defaults.price_table


def test_blank_base_url_with_a_gemini_model_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM__PROVIDER", "openai")
    monkeypatch.setenv("LLM__FAST_MODEL", "gemma-4-26b-a4b-it")
    monkeypatch.setenv("LLM__OPENAI_BASE_URL", "")
    with pytest.raises(ValidationError, match="LLM__OPENAI_BASE_URL is the OpenAI default"):
        AppSettings(_env_file=None)


def test_gemini_configuration_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented Gemini block parses, including the JSON price table (R21.6)."""
    monkeypatch.setenv("LLM__PROVIDER", "openai")
    monkeypatch.setenv("LLM__OPENAI_BASE_URL", GEMINI_BASE_URL)
    monkeypatch.setenv("LLM__FAST_MODEL", "gemma-4-26b-a4b-it")
    monkeypatch.setenv("LLM__STRONG_MODEL", "gemma-4-31b-it")
    monkeypatch.setenv("LLM__FALLBACK_MODEL", "gemini-3.1-flash-lite")
    monkeypatch.setenv("LLM__PRICE_TABLE", json.dumps(GEMINI_PRICE_TABLE))
    llm = AppSettings(_env_file=None).llm
    assert llm.provider == "openai"
    assert llm.strong_model == "gemma-4-31b-it"
    assert llm.price_table["gemini-3.1-flash-lite"] == ModelPricing(
        input_per_m=0.25, output_per_m=1.50
    )
    assert llm.price_table["gemma-4-26b-a4b-it"] == ModelPricing(input_per_m=0, output_per_m=0)


def test_tests_always_run_with_the_fake_llm_provider() -> None:
    """R24.5: the autouse guard pins the fake provider even when the host .env names a real one."""
    assert AppSettings().llm.provider == "fake"


def test_tests_always_run_with_the_mock_embedder() -> None:
    """R24.5: the autouse guard pins EMBEDDING__MOCK=true even when the host .env turns it off."""
    assert AppSettings().embedding.mock is True


def test_business_data_settings_defaults_and_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R13.7 / R20.6: the business_data group validates and reads BUSINESS_DATA__*."""
    defaults = AppSettings(_env_file=None).business_data
    assert (defaults.timeout_ms, defaults.snapshot_orders, defaults.snapshot_tickets) == (500, 3, 3)

    monkeypatch.setenv("BUSINESS_DATA__TIMEOUT_MS", "750")
    monkeypatch.setenv("BUSINESS_DATA__SNAPSHOT_ORDERS", "5")
    monkeypatch.setenv("BUSINESS_DATA__SNAPSHOT_TICKETS", "0")
    overridden = AppSettings(_env_file=None).business_data
    assert (overridden.timeout_ms, overridden.snapshot_orders, overridden.snapshot_tickets) == (
        750,
        5,
        0,
    )


def test_business_data_settings_reject_out_of_range_values() -> None:
    with pytest.raises(ValidationError, match="timeout_ms"):
        BusinessDataSettings(timeout_ms=5)
    with pytest.raises(ValidationError, match="snapshot_orders"):
        BusinessDataSettings(snapshot_orders=-1)
    with pytest.raises(ValidationError, match="snapshot_tickets"):
        BusinessDataSettings(snapshot_tickets=-1)


def test_frontend_settings_defaults_and_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R23.6 / R20.6: the frontend group reads FRONTEND__*; a blank organization is unset."""
    defaults = AppSettings(_env_file=None).frontend
    assert defaults.api_base_url == "http://localhost:8000"
    assert defaults.organization_id is None

    monkeypatch.setenv("FRONTEND__API_BASE_URL", "http://api:8000/")
    monkeypatch.setenv("FRONTEND__ORGANIZATION_ID", "00000000-0000-0000-0000-000000000001")
    overridden = AppSettings(_env_file=None).frontend
    assert overridden.api_base_url == "http://api:8000"
    assert overridden.organization_id == UUID("00000000-0000-0000-0000-000000000001")

    monkeypatch.setenv("FRONTEND__ORGANIZATION_ID", "")
    assert AppSettings(_env_file=None).frontend.organization_id is None


def test_frontend_settings_reject_bad_values() -> None:
    """R20.6: fail fast on a base URL without a scheme or an organization that is not a UUID."""
    with pytest.raises(ValidationError, match="api_base_url"):
        FrontendSettings(api_base_url="localhost:8000")
    with pytest.raises(ValidationError, match="organization_id"):
        FrontendSettings(organization_id="not-a-uuid")


def test_frontend_service_settings_name() -> None:
    assert FrontendServiceSettings(_env_file=None).service_name == "frontend"
