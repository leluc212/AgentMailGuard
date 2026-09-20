"""Validated application settings and service configuration.

Implements requirements:
- R20.6: Read all configuration from environment variables with documented defaults
  and a validated settings object that fails fast on misconfiguration.
- R5.10: Startup assertion for embedding vector dimension.
- R21.6: Model price table for token cost calculation.
"""

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelPricing(BaseModel):
    """Token pricing in USD per 1,000,000 tokens (R21.6)."""

    input_per_m: float = Field(default=0.0, ge=0.0, description="Cost per 1M input tokens in USD")
    output_per_m: float = Field(default=0.0, ge=0.0, description="Cost per 1M output tokens in USD")


class DatabaseSettings(BaseModel):
    """PostgreSQL and pgvector configuration (R5.1)."""

    host: str = Field(default="localhost", description="PostgreSQL host")
    port: int = Field(default=5432, ge=1, le=65535, description="PostgreSQL port")
    user: str = Field(default="postgres", description="PostgreSQL username")
    password: str = Field(default="postgres", description="PostgreSQL password")
    name: str = Field(default="rag_email", description="PostgreSQL database name")
    pool_min: int = Field(default=5, ge=1, description="Minimum connection pool size")
    pool_max: int = Field(default=20, ge=1, description="Maximum connection pool size")
    ssl_mode: str = Field(default="prefer", description="SSL mode for connection")

    @property
    def dsn(self) -> str:
        """Construct standard PostgreSQL connection DSN."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def asyncpg_dsn(self) -> str:
        """Construct asyncpg connection DSN."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


class BrokerSettings(BaseModel):
    """RabbitMQ messaging topology configuration (R3.1, R3.8, R7.1)."""

    host: str = Field(default="localhost", description="RabbitMQ host")
    port: int = Field(default=5672, ge=1, le=65535, description="RabbitMQ AMQP port")
    user: str = Field(default="guest", description="RabbitMQ username")
    password: str = Field(default="guest", description="RabbitMQ password")
    vhost: str = Field(default="/", description="RabbitMQ virtual host")

    # Exchanges (design.md §7.1)
    exchange_mail_ingest: str = Field(
        default="mail.ingest", description="Direct exchange for mail sync requests"
    )
    exchange_email_process: str = Field(
        default="email.process", description="Direct exchange for email normalization"
    )
    exchange_email_triage: str = Field(
        default="email.triage", description="Direct exchange for email triage classification"
    )
    exchange_email_route: str = Field(
        default="email.route", description="Topic exchange for classified emails"
    )
    exchange_email_dispatch: str = Field(
        default="email.dispatch", description="Direct exchange for email response dispatch"
    )
    exchange_knowledge_ingest: str = Field(
        default="knowledge.ingest", description="Direct exchange for knowledge document ingestion"
    )
    exchange_retry: str = Field(
        default="retry.email", description="Direct exchange for retry delay ladder"
    )
    exchange_dlx: str = Field(
        default="dlx.email", description="Topic exchange for terminal dead-lettering"
    )

    # Legacy/convenience aliases
    email_exchange: str = Field(
        default="email.events", description="Topic exchange for email events"
    )
    dlx_exchange: str = Field(default="dlx.email", description="Dead-letter exchange")

    # Queues (design.md §7.1, R3.8)
    queue_mail_sync: str = Field(
        default="mail.sync.requested", description="Mail sync requested queue"
    )
    queue_normalize: str = Field(default="email.normalize", description="Email normalization queue")
    queue_triage: str = Field(default="email.triage", description="Email triage queue")
    queue_dispatch: str = Field(default="email.dispatch", description="Email dispatch queue")
    queue_knowledge: str = Field(
        default="knowledge.ingest", description="Knowledge ingestion queue"
    )
    queue_dead_letter: str = Field(
        default="email.dead_letter", description="Dead-letter queue for terminal failures"
    )

    # Quorum queues support (R3.9)
    use_quorum_queues: bool = Field(
        default=False, description="Enable quorum queues for HA deployment"
    )

    @property
    def url(self) -> str:
        """Construct AMQP connection URL."""
        vhost_part = self.vhost.lstrip("/")
        return f"amqp://{self.user}:{self.password}@{self.host}:{self.port}/{vhost_part}"


class ObjectStorageSettings(BaseModel):
    """MinIO / S3 compatible object storage configuration (R5.8)."""

    endpoint: str = Field(default="localhost:9000", description="MinIO/S3 endpoint host:port")
    access_key: str = Field(default="minioadmin", description="S3 access key")
    secret_key: str = Field(default="minioadmin", description="S3 secret key")
    bucket_raw_mime: str = Field(default="raw-mime", description="Bucket for raw MIME payloads")
    bucket_attachments: str = Field(
        default="attachments", description="Bucket for email attachments"
    )
    bucket_knowledge: str = Field(
        default="knowledge-docs", description="Bucket for uploaded knowledge documents"
    )
    secure: bool = Field(default=False, description="Use HTTPS if True")
    region: str = Field(default="us-east-1", description="S3 region")


class ProviderCredentialsSettings(BaseModel):
    """Credential vault references for external mail providers (R1.1)."""

    mock_providers: bool = Field(
        default=True, description="Enable FakeProviderAdapter for testing/CI"
    )
    gmail_credentials_ref: str = Field(
        default="vault/gmail", description="Secret ref for Gmail OAuth"
    )
    graph_credentials_ref: str = Field(
        default="vault/graph", description="Secret ref for MS Graph OAuth"
    )
    imap_credentials_ref: str = Field(
        default="vault/imap", description="Secret ref for IMAP credentials"
    )


class EmbeddingSettings(BaseModel):
    """Embedding model and dimensionality configuration (R5.10)."""

    model_name: str = Field(
        default="text-embedding-3-small", description="Embedding model identifier"
    )
    dimension: int = Field(
        default=1536, ge=1, le=10000, description="Embedding vector dimensionality"
    )
    batch_size: int = Field(default=64, ge=1, description="Embedding inference batch size")
    max_tokens: int = Field(default=8191, ge=1, description="Maximum context length for embeddings")


class LLMTiersSettings(BaseModel):
    """Tiered LLM routing and token price table (R15.1, R21.6)."""

    fast_model: str = Field(
        default="gpt-4o-mini", description="Tier 1 fast model for triage/summarization"
    )
    strong_model: str = Field(
        default="gpt-4o", description="Tier 2 strong model for high-confidence draft generation"
    )
    fallback_model: str = Field(default="claude-3-haiku", description="Tier 3 fallback model")
    force_single_tier: bool = Field(
        default=False, description="Force strong model only for ablation study (R15.6)"
    )

    price_table: dict[str, ModelPricing] = Field(
        default_factory=lambda: {
            "gpt-4o-mini": ModelPricing(input_per_m=0.15, output_per_m=0.60),
            "gpt-4o": ModelPricing(input_per_m=5.00, output_per_m=15.00),
            "claude-3-haiku": ModelPricing(input_per_m=0.25, output_per_m=1.25),
            "text-embedding-3-small": ModelPricing(input_per_m=0.02, output_per_m=0.0),
        },
        description="Per-model token pricing in USD per 1M tokens",
    )


class RetrievalSettings(BaseModel):
    """Hybrid search and reranking parameters (R10, R11)."""

    top_n: int = Field(default=20, ge=1, le=100, description="Candidate chunks per search branch")
    rrf_k: int = Field(default=60, ge=1, description="Reciprocal Rank Fusion k constant")
    top_k: int = Field(
        default=5, ge=1, le=50, description="Final context chunks provided to generation"
    )
    rerank_enabled: bool = Field(default=True, description="Enable cross-encoder reranking")
    relevance_floor: float = Field(
        default=0.70, ge=0.0, le=1.0, description="Minimum rerank relevance score"
    )
    retrieval_timeout_ms: int = Field(
        default=500, ge=10, description="Retrieval SLA timeout in milliseconds"
    )


class TriageSettings(BaseModel):
    """Cascading triage thresholds and routing floors (R6.1, R6.2)."""

    rule_confidence_threshold: float = Field(
        default=0.95, ge=0.0, le=1.0, description="Rule engine cutoff"
    )
    ml_confidence_threshold: float = Field(
        default=0.80, ge=0.0, le=1.0, description="Lightweight ML classifier cutoff"
    )
    llm_confidence_threshold: float = Field(
        default=0.70, ge=0.0, le=1.0, description="LLM classifier cutoff"
    )
    safe_default_category: str = Field(
        default="general_inquiry", description="Fallback category if all stages fail (R6.11)"
    )
    safe_default_priority: str = Field(
        default="normal", description="Fallback priority if all stages fail"
    )

    @model_validator(mode="after")
    def validate_threshold_order(self) -> "TriageSettings":
        """Assert rule threshold is higher than or equal to ML and LLM floors."""
        if self.rule_confidence_threshold < self.ml_confidence_threshold:
            raise ValueError("rule_confidence_threshold must be >= ml_confidence_threshold")
        return self


class SummarizationSettings(BaseModel):
    """Thread conversation summarization thresholds (R8.3, R8.4)."""

    min_messages_threshold: int = Field(
        default=4, ge=1, description="Thread message count triggering summarization"
    )
    context_token_threshold: int = Field(
        default=1500, ge=100, description="Token estimate triggering summarization"
    )
    keep_latest_messages: int = Field(
        default=2, ge=1, description="Number of verbatim messages kept alongside summary"
    )
    summarizer_model: str = Field(
        default="gpt-4o-mini", description="Model used for generating conversation summaries"
    )


class ThreadAssociationSettings(BaseModel):
    """Configuration for message-to-thread association rules (R4.6, design.md §5.2)."""

    window_days: int = Field(
        default=14,
        ge=1,
        le=365,
        description="Time window in days for matching threads by subject and participants",
    )


class RetryLadderSettings(BaseModel):
    """Exponential message queue retry intervals (R3.4)."""

    tier_1_delay_s: int = Field(default=30, ge=1, description="First retry delay in seconds")
    tier_2_delay_s: int = Field(default=300, ge=1, description="Second retry delay in seconds (5m)")
    tier_3_delay_s: int = Field(
        default=1800, ge=1, description="Third retry delay in seconds (30m)"
    )
    max_retries: int = Field(
        default=3, ge=1, le=10, description="Max delivery attempts before dead-lettering"
    )

    @model_validator(mode="after")
    def validate_retry_progression(self) -> "RetryLadderSettings":
        """Verify delays are strictly ascending."""
        if not (self.tier_1_delay_s < self.tier_2_delay_s < self.tier_3_delay_s):
            raise ValueError(
                "Retry delay progression must be strictly ascending: tier_1 < tier_2 < tier_3"
            )
        return self


class WorkerConcurrencySettings(BaseModel):
    """Per-service queue consumer prefetch and worker concurrency limits (R20.3)."""

    default_prefetch: int = Field(default=10, ge=1, description="Default RabbitMQ prefetch count")
    mail_connector_concurrency: int = Field(
        default=5, ge=1, description="Mail connector sync workers"
    )
    email_worker_concurrency: int = Field(default=10, ge=1, description="Email normalizer workers")
    triage_worker_concurrency: int = Field(
        default=10, ge=1, description="Triage classifier workers"
    )
    ai_worker_concurrency: int = Field(default=4, ge=1, description="AI generation workers")
    knowledge_worker_concurrency: int = Field(
        default=2, ge=1, description="Knowledge ingestion workers"
    )
    dispatch_worker_concurrency: int = Field(
        default=5, ge=1, description="Dispatch response workers"
    )


class ObservabilitySettings(BaseModel):
    """Logging, OpenTelemetry tracing, Prometheus metrics, and service health settings.

    Fulfills requirements: R21, R20.7, R20.8.
    """

    log_level: str = Field(
        default="INFO", description="Standard logging level (DEBUG, INFO, WARNING, ERROR)"
    )
    log_format: str = Field(default="json", description="Log output format: 'json' or 'text'")
    otlp_endpoint: str | None = Field(
        default=None, description="OpenTelemetry collector OTLP gRPC/HTTP endpoint"
    )
    prometheus_enabled: bool = Field(
        default=True, description="Enable Prometheus metric collection"
    )
    metrics_port: int = Field(
        default=9090, ge=1, le=65535, description="Port for worker metrics & health HTTP server"
    )
    health_port: int = Field(
        default=8080, ge=1, le=65535, description="Port for worker health checks"
    )
    drain_timeout_s: float = Field(
        default=15.0, ge=1.0, description="Graceful shutdown drain timeout in seconds"
    )


class SubscriptionRenewalSettings(BaseModel):
    """Settings for scheduled provider subscription renewal (R2.10)."""

    renewal_threshold_hours: int = Field(
        default=24,
        ge=1,
        description="Renew subscriptions expiring within this number of hours",
    )
    check_interval_seconds: int = Field(
        default=3600,
        ge=10,
        description="Interval between renewal check cycles in seconds",
    )
    batch_size: int = Field(
        default=100,
        ge=1,
        description="Maximum subscriptions evaluated per renewal batch",
    )


class AppSettings(BaseSettings):
    """Top-level master settings container supporting environment variable loading."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    environment: str = Field(
        default="development", description="Environment: development, test, production"
    )
    service_name: str = Field(default="rag-email", description="Current service name")
    debug: bool = Field(default=False, description="Enable debug mode")

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    broker: BrokerSettings = Field(default_factory=BrokerSettings)
    object_storage: ObjectStorageSettings = Field(default_factory=ObjectStorageSettings)
    providers: ProviderCredentialsSettings = Field(default_factory=ProviderCredentialsSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    llm: LLMTiersSettings = Field(default_factory=LLMTiersSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    triage: TriageSettings = Field(default_factory=TriageSettings)
    summarization: SummarizationSettings = Field(default_factory=SummarizationSettings)
    thread_association: ThreadAssociationSettings = Field(default_factory=ThreadAssociationSettings)
    retry: RetryLadderSettings = Field(default_factory=RetryLadderSettings)
    concurrency: WorkerConcurrencySettings = Field(default_factory=WorkerConcurrencySettings)
    telemetry: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    subscription_renewal: SubscriptionRenewalSettings = Field(
        default_factory=SubscriptionRenewalSettings
    )


def assert_embedding_dimension(configured_dim: int, db_column_dim: int) -> None:
    """Validate configured embedding dimension matches database column definition (R5.10).

    Raises:
        ValueError: If configured dimension differs from database column width.
    """
    if configured_dim != db_column_dim:
        raise ValueError(
            f"Configured embedding dimension ({configured_dim}) does not match "
            f"database column width ({db_column_dim}). Refusing to start."
        )


# Specialized service settings classes
class APISettings(AppSettings):
    """Settings specialized for the API service."""

    service_name: str = "api"
    api_prefix: str = "/v1"
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://localhost:3001",
            "http://localhost:8000",
        ],
        description="Allowed origins for CORS requests.",
    )
    title: str = "Enterprise RAG Email API"
    version: str = "0.1.0"
    description: str = (
        "Operator REST API for Enterprise RAG-Based Intelligent "
        "Email Management and Response System"
    )


class MailConnectorSettings(AppSettings):
    """Settings specialized for the mail connector."""

    service_name: str = "mail_connector"


class EmailWorkerSettings(AppSettings):
    """Settings specialized for the email normalization worker."""

    service_name: str = "email_worker"


class TriageWorkerSettings(AppSettings):
    """Settings specialized for the triage worker."""

    service_name: str = "triage_worker"


class AIWorkerSettings(AppSettings):
    """Settings specialized for the AI generation worker."""

    service_name: str = "ai_worker"


class KnowledgeWorkerSettings(AppSettings):
    """Settings specialized for the knowledge ingestion worker."""

    service_name: str = "knowledge_worker"


class DispatchWorkerSettings(AppSettings):
    """Settings specialized for the dispatch worker."""

    service_name: str = "dispatch_worker"
