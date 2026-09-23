"""Unit tests for configuration and settings management (R20.6, R5.10, R21.6)."""

import pytest
from pydantic import ValidationError

from packages.core.settings import (
    AIWorkerSettings,
    APISettings,
    AppSettings,
    DatabaseSettings,
    DispatchWorkerSettings,
    EmailWorkerSettings,
    KnowledgeWorkerSettings,
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

