"""Validated application settings and service configuration.

Implements requirements:
- R20.6: Read all configuration from environment variables with documented defaults
  and a validated settings object that fails fast on misconfiguration.
- R5.10: Startup assertion for embedding vector dimension.
- R21.6: Model price table for token cost calculation.
"""

import os
import re
from typing import Any
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
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


# Category routing queues bound on the topic exchange: email.<category>.<lane>
# (packages/broker/topology.py category bindings; lanes per CategoryRoutingSettings defaults).
_CATEGORY_QUEUE_RE = re.compile(r"email\.[a-z0-9_]+\.(?:normal|priority)")


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
    exchange_retry_return: str = Field(
        default="retry.return",
        description=(
            "Headers exchange that expired retry messages dead-letter into; it routes each "
            "message back to its origin exchange via the retry-origin-exchange header "
            "(design.md §7.2)"
        ),
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

    def exchange_for_queue(self, queue_name: str | None) -> str | None:
        """Return the exchange whose binding delivers ``queue_name`` as routing key.

        Mirrors the bindings declared by ``packages.broker.topology.setup_topology``.
        Returns ``None`` for unknown, retry, or dead-letter queues, so callers refuse to
        publish instead of emitting a message the broker would drop as unroutable.
        """
        if not queue_name:
            return None
        direct_bindings = {
            self.queue_mail_sync: self.exchange_mail_ingest,
            self.queue_normalize: self.exchange_email_process,
            self.queue_triage: self.exchange_email_triage,
            self.queue_dispatch: self.exchange_email_dispatch,
            self.queue_knowledge: self.exchange_knowledge_ingest,
        }
        if queue_name in direct_bindings:
            return direct_bindings[queue_name]
        if _CATEGORY_QUEUE_RE.fullmatch(queue_name):
            return self.exchange_email_route
        return None


class ObjectStorageSettings(BaseModel):
    """MinIO / S3 compatible object storage configuration (R5.8)."""

    endpoint: str = Field(default="localhost:9000", description="MinIO/S3 endpoint host:port")
    access_key: str = Field(default="minioadmin", description="S3 access key")
    secret_key: str = Field(default="minioadmin", description="S3 secret key")
    bucket_raw_mime: str = Field(default="raw-mime", description="Bucket for raw MIME payloads")
    bucket_attachments: str = Field(
        default="attachments", description="Bucket for email attachments"
    )
    bucket_html: str = Field(default="html", description="Bucket for parsed HTML email bodies")
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
    """Embedding model and dimensionality configuration (R5.10, R9.6, R9.11)."""

    model_name: str = Field(
        default="text-embedding-3-small", description="Embedding model identifier"
    )
    dimension: int = Field(
        default=1536, ge=1, le=10000, description="Embedding vector dimensionality"
    )
    batch_size: int = Field(default=64, ge=1, description="Embedding inference batch size")
    max_tokens: int = Field(default=8191, ge=1, description="Maximum context length for embeddings")
    base_url: str = Field(
        default="https://api.openai.com/v1",
        description="Base URL for OpenAI-compatible embedding API",
    )
    api_key: str | None = Field(default=None, description="API key for embedding service")
    mock: bool = Field(default=True, description="Enable FakeEmbedder for testing/CI (R24.5)")
    timeout_s: float = Field(
        default=10.0, ge=0.1, description="Embedding request timeout in seconds"
    )
    max_retries: int = Field(
        default=3, ge=0, description="Maximum retry attempts on transient errors"
    )
    retry_delay_s: float = Field(default=0.5, ge=0.0, description="Initial retry delay in seconds")


OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
"""The `openai` provider's default endpoint; a blank LLM__OPENAI_BASE_URL resolves to it."""

GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
"""Google's OpenAI-compatible Gemini endpoint, used by the project's live runs (task 5.0)."""

_GOOGLE_MODEL_MARKERS = ("gemini", "gemma")


class ProviderRouting(BaseModel):
    """OpenRouter's ``provider`` request object, limited to the keys a pin needs.

    A pinned route sends ``order`` (one provider slug), ``allow_fallbacks: false`` and
    ``require_parameters: true``, plus ``quantizations`` when the endpoint reports its precision
    (Qwen2.5-7B's only provider reports ``unknown``, so a filter would exclude it). OpenRouter's
    schema refuses unknown provider keys, so this model refuses them too.
    """

    model_config = ConfigDict(extra="forbid")

    order: list[str] = Field(min_length=1, description="Provider slugs, tried in order")
    allow_fallbacks: bool = Field(
        default=False, description="False: an unavailable provider is an error, never a substitute"
    )
    require_parameters: bool = Field(
        default=True, description="Route only to endpoints that support every request parameter"
    )
    quantizations: list[str] | None = Field(
        default=None, description="Accepted precisions, e.g. ['bf16']; None: no filter"
    )

    def request_object(self) -> dict[str, Any]:
        """The value of the request body's ``provider`` field."""
        return self.model_dump(exclude_none=True)


class LLMTiersSettings(BaseModel):
    """Tiered LLM routing, provider configuration, and token price table.

    Covers R14.7, R15.1, R21.6, and R24.5.
    """

    provider: str = Field(
        default="fake",
        description="LLM provider implementation: fake | openai | anthropic | local (R14.7, R24.5)",
    )
    openai_api_key: str | None = Field(default=None, description="OpenAI API key")
    openai_base_url: str = Field(
        default=OPENAI_DEFAULT_BASE_URL,
        description=(
            f"OpenAI-compatible API base URL; the Gemini API uses {GEMINI_OPENAI_BASE_URL} (R14.7)"
        ),
    )
    openai_provider_routing: ProviderRouting | None = Field(
        default=None,
        description=(
            "OpenRouter provider routing sent as the request's `provider` object (JSON); pins the "
            "serving provider. Blank = not routed (R14.7)"
        ),
    )
    openai_response_metadata: bool = Field(
        default=False,
        description=(
            "Ask the router for its metadata (X-OpenRouter-Metadata: enabled) so each call "
            "records which provider served it; required with a routing pin"
        ),
    )
    anthropic_api_key: str | None = Field(default=None, description="Anthropic API key")
    anthropic_base_url: str = Field(
        default="https://api.anthropic.com/v1", description="Anthropic API base URL"
    )
    local_base_url: str = Field(
        default="http://localhost:11434/v1", description="Local OpenAI-compatible base URL"
    )
    local_api_key: str = Field(default="ollama", description="Local endpoint API key")
    timeout_s: float = Field(default=15.0, ge=0.1, description="LLM request timeout in seconds")

    model_config = ConfigDict(populate_by_name=True)

    fast_model: str = Field(
        default="gpt-4o-mini",
        validation_alias=AliasChoices("fast_model", "routine_model"),
        description="Tier 1 fast/routine model for triage/summarization/drafting",
    )
    strong_model: str = Field(
        default="gpt-4o",
        validation_alias=AliasChoices("strong_model", "high_capability_model"),
        description="Tier 2 strong/high-capability model for high-confidence draft generation",
    )
    fallback_model: str = Field(default="claude-3-haiku", description="Tier 3 fallback model")
    force_single_tier: bool = Field(
        default=False, description="Force strong model only for ablation study (R15.6)"
    )

    @property
    def routine_model(self) -> str:
        """Alias property for fast_model to support routine tier naming (R15.1)."""
        return self.fast_model

    @property
    def high_capability_model(self) -> str:
        """Alias property for strong_model to support high_capability tier naming (R15.1)."""
        return self.strong_model

    price_table: dict[str, ModelPricing] = Field(
        default_factory=lambda: {
            "gpt-4o-mini": ModelPricing(input_per_m=0.15, output_per_m=0.60),
            "gpt-4o": ModelPricing(input_per_m=5.00, output_per_m=15.00),
            "claude-3-haiku": ModelPricing(input_per_m=0.25, output_per_m=1.25),
            "text-embedding-3-small": ModelPricing(input_per_m=0.02, output_per_m=0.0),
        },
        description="Per-model token pricing in USD per 1M tokens",
    )

    @field_validator(
        "openai_base_url",
        "fast_model",
        "strong_model",
        "fallback_model",
        "price_table",
        "openai_provider_routing",
        "openai_response_metadata",
        mode="before",
    )
    @classmethod
    def _blank_means_default(cls, value: Any, info: ValidationInfo) -> Any:
        """Treat a blank value as unset (R20.6).

        Docker Compose forwards an unset host variable as an empty string. An empty model
        name or price table is never a valid setting, and an empty base URL would otherwise
        make the OpenAI client fall back to api.openai.com silently.
        """
        if isinstance(value, str) and not value.strip() and info.field_name is not None:
            return cls.model_fields[info.field_name].get_default(call_default_factory=True)
        return value

    @model_validator(mode="after")
    def validate_route_is_verifiable(self) -> "LLMTiersSettings":
        """A provider pin is only a pin when each response can be checked against it."""
        if self.openai_provider_routing is not None and not self.openai_response_metadata:
            raise ValueError(
                "LLM__OPENAI_PROVIDER_ROUTING pins a provider, so LLM__OPENAI_RESPONSE_METADATA "
                "must be true: without the router's metadata the served provider is unknown"
            )
        return self

    @model_validator(mode="after")
    def validate_openai_endpoint_matches_models(self) -> "LLMTiersSettings":
        """Fail fast when the openai provider would send a Google key to OpenAI (R20.6, R14.7).

        Gemini and Gemma models are served by Google's OpenAI-compatible endpoint. Naming one
        while LLM__OPENAI_BASE_URL is still the OpenAI default means the configured key would
        be sent to the wrong host, so startup refuses the configuration instead.
        """
        if self.provider.lower().strip() != "openai":
            return self
        if self.openai_base_url.rstrip("/") != OPENAI_DEFAULT_BASE_URL:
            return self
        google_models = [
            model
            for model in (self.fast_model, self.strong_model, self.fallback_model)
            if any(marker in model.lower() for marker in _GOOGLE_MODEL_MARKERS)
        ]
        if google_models:
            raise ValueError(
                "LLM__PROVIDER=openai names Gemini/Gemma model(s) "
                f"{', '.join(google_models)} but LLM__OPENAI_BASE_URL is the OpenAI default; "
                f"set LLM__OPENAI_BASE_URL={GEMINI_OPENAI_BASE_URL}"
            )
        return self


class RetrievalSettings(BaseModel):
    """Hybrid search and reranking parameters (R10, R11)."""

    top_n: int = Field(default=20, ge=1, le=100, description="Candidate chunks per search branch")
    rrf_k: int = Field(default=60, ge=1, description="Reciprocal Rank Fusion k constant")
    top_k: int = Field(
        default=5, ge=1, le=50, description="Final context chunks provided to generation"
    )
    rerank_enabled: bool = Field(default=True, description="Enable cross-encoder reranking")
    rerank_model: str = Field(
        default="cross-encoder/ms-marco-MiniLM-L-6-v2",
        description="Cross-encoder model that reranks the fused candidates (R11.1)",
    )
    rerank_model_dir: str = Field(
        default="",
        description=(
            "Directory holding the reranker model in the Hugging Face cache layout; the model "
            "is then loaded from it with no network. Empty uses the default Hugging Face cache "
            "(R11.1)"
        ),
    )
    rerank_timeout_ms: int = Field(
        default=1000,
        ge=10,
        description="Rerank budget in milliseconds; a slower rerank keeps RRF order (R11.5)",
    )

    @field_validator("rerank_model", mode="before")
    @classmethod
    def _blank_rerank_model_means_default(cls, value: Any) -> Any:
        """Treat a blank reranker model as unset (R20.6).

        Docker Compose forwards an unset host variable as an empty string, and an empty model
        name would only fail at the first rerank.
        """
        if isinstance(value, str) and not value.strip():
            return cls.model_fields["rerank_model"].get_default()
        return value

    relevance_floor: float = Field(
        default=0.70, ge=0.0, le=1.0, description="Minimum rerank relevance score"
    )
    retrieval_timeout_ms: int = Field(
        default=3000, ge=10, description="Retrieval SLA timeout in milliseconds"
    )
    category_filter_enabled: bool = Field(
        default=True,
        description=(
            "Filter retrieval by the category triage assigned (R10.4, R12.4). False searches "
            "every active document of the tenant whatever its category; the organization and "
            "status filters stay. Only the live benchmark turns it off"
        ),
    )


class BusinessDataSettings(BaseModel):
    """Business-data lookup deadline and snapshot sizes (R13.4, R13.7, design.md §5.4)."""

    timeout_ms: int = Field(
        default=500,
        ge=10,
        description=(
            "Deadline for one business-data provider call in milliseconds; also the Postgres "
            "statement_timeout (R13.7)"
        ),
    )
    snapshot_orders: int = Field(
        default=3, ge=0, description="Most recent orders in a customer snapshot (design.md §5.4)"
    )
    snapshot_tickets: int = Field(
        default=3,
        ge=0,
        description="Open tickets (not closed or resolved) in a customer snapshot (design.md §5.4)",
    )


class FrontendSettings(BaseModel):
    """Review UI connection to the /v1 API (R23.4-R23.7, design.md §5.8, ADR-0009)."""

    api_base_url: str = Field(
        default="http://localhost:8000",
        description="Base URL of the API the review UI calls; only /v1 paths are used (R23.6)",
    )
    organization_id: UUID | None = Field(
        default=None,
        description=(
            "Tenant whose drafts the review UI shows, sent as the X-Organization-Id header "
            "(R23.6); unset renders a setup error instead of calling the API"
        ),
    )

    @field_validator("api_base_url")
    @classmethod
    def _require_http_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("api_base_url must start with http:// or https://")
        return cleaned

    @field_validator("organization_id", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


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
    rules_path: str = Field(
        default="config/triage_rules.yaml",
        description="Path to declarative triage rules YAML (R6.8)",
    )
    ml_model_path: str = Field(
        default="artifacts/models/triage_ml_v1.joblib",
        description="Path to trained ML classifier model artifact (R6.1)",
    )
    templates_path: str = Field(
        default="config/templates.yaml",
        description="Path to declarative response templates YAML (R6.13, R6.14)",
    )
    category_retrieval_floor: bool = Field(
        default=True,
        description=(
            "A required reply routed to AI generation retrieves when its category's "
            "default_retrieval_required (config/categories.yaml) is true, whatever the triage "
            "stage said; false keeps each stage's own retrieval_required (R6.6, R12.4)"
        ),
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
    resummarize_lag_messages: int = Field(
        default=2,
        ge=0,
        description=(
            "Re-summarize only after more than this many messages arrived since "
            "summarized_through_message_id (R8.4, design.md §5.4 LAG)"
        ),
    )
    summarizer_model: str | None = Field(
        default=None,
        description=(
            "Model that writes conversation summaries (R8.3); unset means the FAST tier model, "
            "so a model is only ever sent to the endpoint when the operator names one"
        ),
    )

    @field_validator("summarizer_model", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        """Treat a blank value as unset: Docker Compose forwards an unset variable as ""."""
        if isinstance(value, str):
            return value.strip() or None
        return value


class ThreadAssociationSettings(BaseModel):
    """Configuration for message-to-thread association rules (R4.6, design.md §5.2)."""

    window_days: int = Field(
        default=14,
        ge=1,
        le=365,
        description="Time window in days for matching threads by subject and participants",
    )


class RetryLadderSettings(BaseModel):
    """Exponential message queue retry intervals (R3.4, R19.5)."""

    tier_1_delay_s: int = Field(default=30, ge=1, description="First retry delay in seconds")
    tier_2_delay_s: int = Field(default=300, ge=1, description="Second retry delay in seconds (5m)")
    tier_3_delay_s: int = Field(
        default=1800, ge=1, description="Third retry delay in seconds (30m)"
    )
    max_retries: int = Field(
        default=3, ge=1, le=10, description="Max delivery attempts before dead-lettering"
    )
    backoff_base_s: float = Field(
        default=1.0, ge=0.01, description="Base backoff interval in seconds"
    )
    backoff_factor: float = Field(
        default=2.0, ge=1.0, description="Exponential backoff multiplication factor"
    )
    max_backoff_s: float = Field(
        default=1800.0, ge=1.0, description="Maximum ceiling backoff delay in seconds"
    )
    jitter_mode: str = Field(
        default="full",
        description="Jitter strategy ('full', 'equal', 'decorrelated', 'none')",
    )

    @model_validator(mode="after")
    def validate_retry_progression(self) -> "RetryLadderSettings":
        """Verify delays are strictly ascending."""
        if not (self.tier_1_delay_s < self.tier_2_delay_s < self.tier_3_delay_s):
            raise ValueError(
                "Retry delay progression must be strictly ascending: tier_1 < tier_2 < tier_3"
            )
        if self.jitter_mode not in {"full", "equal", "decorrelated", "none"}:
            raise ValueError(
                f"Invalid jitter_mode '{self.jitter_mode}'. "
                "Must be one of 'full', 'equal', 'decorrelated', 'none'."
            )
        return self


class WorkerConcurrencySettings(BaseModel):
    """Per-service queue consumer prefetch and worker concurrency limits (R20.3, R7.2)."""

    default_prefetch: int = Field(default=10, ge=1, description="Default RabbitMQ prefetch count")
    mail_connector_concurrency: int = Field(
        default=5, ge=1, description="Mail connector sync workers"
    )
    email_worker_concurrency: int = Field(default=10, ge=1, description="Email normalizer workers")
    triage_worker_concurrency: int = Field(
        default=10, ge=1, description="Triage classifier workers"
    )
    ai_worker_concurrency: int = Field(default=4, ge=1, description="AI generation workers (base)")
    ai_worker_normal_concurrency: int = Field(
        default=4, ge=1, description="AI workers for normal priority lane (R7.2)"
    )
    ai_worker_priority_concurrency: int = Field(
        default=8, ge=1, description="AI workers for priority lane (R7.2)"
    )
    ai_worker_normal_prefetch: int = Field(
        default=10, ge=1, description="RabbitMQ prefetch for normal lane (R7.2)"
    )
    ai_worker_priority_prefetch: int = Field(
        default=5, ge=1, description="RabbitMQ prefetch for priority lane (R7.2)"
    )
    knowledge_worker_concurrency: int = Field(
        default=2, ge=1, description="Knowledge ingestion workers"
    )
    dispatch_worker_concurrency: int = Field(
        default=5, ge=1, description="Dispatch response workers"
    )
    micro_batch_size: int = Field(
        default=5, ge=1, description="Number of jobs pulled together in a micro-batch (R3.6)"
    )
    micro_batch_timeout_ms: int = Field(
        default=50, ge=1, le=5000, description="Max wait time in ms to fill micro-batch (R3.6)"
    )

    @model_validator(mode="after")
    def validate_micro_batch_bounds(self) -> "WorkerConcurrencySettings":
        """Verify micro_batch_size does not exceed prefetch limits."""
        if self.micro_batch_size > self.default_prefetch:
            raise ValueError(
                f"micro_batch_size ({self.micro_batch_size}) cannot exceed "
                f"default_prefetch ({self.default_prefetch})"
            )
        return self


class CategoryRoutingSettings(BaseModel):
    """Declarative category queues, priority lanes, and unconsumed queue alerts (R7.1–R7.6)."""

    categories_config_path: str = Field(
        default="config/categories.yaml",
        description="Path to declarative category taxonomy YAML file (R7.4)",
    )
    priority_lanes: list[str] = Field(
        default_factory=lambda: ["normal", "priority"],
        description="Declared priority routing lanes (R7.2)",
    )
    configured_consumers: list[str] | None = Field(
        default=None,
        description=(
            "Queues or glob patterns that have active consumers (R7.6). Unset (the default) "
            "derives them from the category taxonomy: every category on every lane, so no lane "
            "is left without a consumer (v2 Amendment 1, G.2). A value, even [], is used as is. "
            "Read it with packages.broker.routing.resolve_configured_consumers"
        ),
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


class LeaseReaperSettings(BaseModel):
    """Lease reaper configuration for recovering stuck jobs (R19.8, design.md §9)."""

    enabled: bool = Field(
        default=False,
        description=(
            "Enable the background lease reaper in mail-connector. Off by default: leases are "
            "not yet cleared on completion and processing_job.queue_name is not yet written, so "
            "enabling it would re-drive parked jobs (see RA.10 notes)"
        ),
    )
    lease_timeout_s: int = Field(
        default=300, ge=10, description="Lease duration in seconds before a job is considered stuck"
    )
    reaper_interval_s: float = Field(
        default=30.0, ge=0.01, description="Interval between reaper sweeps in seconds"
    )
    batch_size: int = Field(
        default=100, ge=1, le=1000, description="Maximum number of expired jobs reaped per sweep"
    )
    reap_stuck_unleased: bool = Field(
        default=True,
        description="Whether to also reclaim jobs in active states with NULL lease past timeout",
    )


class AgentProfileSettings(BaseModel):
    """Configuration for agent profile registry and prompt templates (R14.1, R14.2)."""

    config_path: str = Field(
        default="config/agent_profiles.yaml",
        description="Path to YAML agent profile definitions",
    )
    default_profile: str = Field(
        default="general_inquiry",
        description="Fallback agent profile when classification category is unmapped",
    )
    prompts_dir: str = Field(
        default="prompts",
        description="Base directory for prompt templates",
    )
    schemas_dir: str = Field(
        default="schemas",
        description="Base directory for structured JSON schemas",
    )


class ComplexityRouterSettings(BaseModel):
    """Configuration for ComplexityRouter and model cascade escalation rules.

    References: R15.1, R15.6, design.md §5.7.
    """

    enabled: bool = Field(
        default=True,
        description="Enable complexity-based model cascade routing (R15.1)",
    )
    force_single_tier: bool = Field(
        default=False,
        description="Bypass cascade and force single tier for H4 empirical evaluation (R15.6)",
    )
    default_tier: str = Field(
        default="routine",
        description="Default routine tier for standard email processing (R15.1, R15.2)",
    )
    escalated_tier: str = Field(
        default="high_capability",
        description="Target tier when complexity triggers escalate (R15.1, R15.3)",
    )
    confidence_threshold: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        description="Triage confidence score threshold below which generation escalates (R15.3)",
    )
    thread_messages_threshold: int = Field(
        default=5,
        ge=1,
        description="Thread message count threshold triggering escalation (R15.3)",
    )
    thread_tokens_threshold: int = Field(
        default=2000,
        ge=1,
        description="Thread token estimate threshold triggering escalation (R15.3)",
    )
    min_retrieved_chunks: int = Field(
        default=2,
        ge=0,
        description="Minimum relevant chunks required to avoid escalation (R15.3)",
    )
    min_relevance_score: float = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum relevance score (a probability) a reranked chunk must reach; applies only "
            "when the cross-encoder rerank ran, never to RRF-ordered chunks (R15.3, task 7.21)"
        ),
    )
    multiple_actions_threshold: int = Field(
        default=2,
        ge=1,
        description="Count of detected requested actions triggering escalation (R15.3)",
    )
    context_tokens_threshold: int = Field(
        default=3500,
        ge=1,
        description="Total context token estimate triggering escalation (R15.3)",
    )
    max_escalations_per_job: int = Field(
        default=1,
        ge=0,
        description="Maximum allowed escalations per job to prevent retry loops (R15.5)",
    )
    single_tier_override: str = Field(
        default="high_capability",
        description="Model tier to use when force_single_tier is enabled (R15.6)",
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
    routing: CategoryRoutingSettings = Field(default_factory=CategoryRoutingSettings)
    telemetry: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    subscription_renewal: SubscriptionRenewalSettings = Field(
        default_factory=SubscriptionRenewalSettings
    )
    lease_reaper: LeaseReaperSettings = Field(default_factory=LeaseReaperSettings)
    agent_profiles: AgentProfileSettings = Field(default_factory=AgentProfileSettings)
    business_data: BusinessDataSettings = Field(default_factory=BusinessDataSettings)
    frontend: FrontendSettings = Field(default_factory=FrontendSettings)
    complexity_router: ComplexityRouterSettings = Field(
        default_factory=ComplexityRouterSettings,
        validation_alias=AliasChoices("complexity_router", "router"),
        description="Configuration for complexity routing and model cascading (R15.1, R15.6)",
    )

    @model_validator(mode="before")
    @classmethod
    def _populate_router_env(cls, data: Any) -> Any:
        """Support flat ROUTER_* or ROUTER__* env vars for complexity_router."""
        if not isinstance(data, dict):
            return data

        existing = data.get("complexity_router")
        if existing is None:
            existing = data.get("router")
        if isinstance(existing, ComplexityRouterSettings):
            return data

        router_dict = dict(existing) if isinstance(existing, dict) else {}

        for source in (data, os.environ):
            for k, v in source.items():
                k_upper = k.upper()
                if k_upper.startswith("ROUTER_") and not k_upper.startswith("ROUTER__"):
                    field_name = k_upper[len("ROUTER_") :].lower()
                    if (
                        field_name in ComplexityRouterSettings.model_fields
                        and field_name not in router_dict
                    ):
                        router_dict[field_name] = v
                elif k_upper.startswith("ROUTER__"):
                    field_name = k_upper[len("ROUTER__") :].lower()
                    if (
                        field_name in ComplexityRouterSettings.model_fields
                        and field_name not in router_dict
                    ):
                        router_dict[field_name] = v
                elif k_upper.startswith("COMPLEXITY_ROUTER__"):
                    field_name = k_upper[len("COMPLEXITY_ROUTER__") :].lower()
                    if (
                        field_name in ComplexityRouterSettings.model_fields
                        and field_name not in router_dict
                    ):
                        router_dict[field_name] = v

        if router_dict:
            data["complexity_router"] = router_dict
        return data


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


class FrontendServiceSettings(AppSettings):
    """Settings specialized for the review UI service (design.md §5.8, ADR-0009)."""

    service_name: str = "frontend"
