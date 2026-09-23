"""Broker messaging foundation package (R3.1, R3.2, R3.3, R3.4, R3.5, R3.8).

Exports topology declaration, standard job envelopes, persistent publishers,
and base consumer abstractions.
"""

from packages.broker.backoff import calculate_exponential_backoff, resolve_retry_tier_delay
from packages.broker.batch_consumer import BaseBatchConsumer, BatchItem
from packages.broker.consumer import BaseConsumer, FatalError, TransientError
from packages.broker.envelope import JobEnvelope
from packages.broker.prompt_safety import (
    PromptContaminationError,
    PromptExecutionRecord,
    assert_prompt_isolation,
)
from packages.broker.publisher import MessagePublisher
from packages.broker.retry import (
    handle_job_recovery,
    handle_job_terminal_failure,
    handle_job_transient_failure,
)
from packages.broker.routing import (
    CANONICAL_LANES,
    HIGH_PRIORITY_LEVELS,
    format_routing_key,
    is_queue_consumed,
    load_categories_from_yaml,
    prepare_route_envelope,
    resolve_priority_lane,
)
from packages.broker.topology import BrokerTopology, setup_topology

__all__ = [
    "BaseBatchConsumer",
    "BaseConsumer",
    "BatchItem",
    "BrokerTopology",
    "CANONICAL_LANES",
    "FatalError",
    "HIGH_PRIORITY_LEVELS",
    "JobEnvelope",
    "MessagePublisher",
    "PromptContaminationError",
    "PromptExecutionRecord",
    "TransientError",
    "assert_prompt_isolation",
    "calculate_exponential_backoff",
    "format_routing_key",
    "handle_job_recovery",
    "handle_job_terminal_failure",
    "handle_job_transient_failure",
    "is_queue_consumed",
    "load_categories_from_yaml",
    "prepare_route_envelope",
    "resolve_priority_lane",
    "resolve_retry_tier_delay",
    "setup_topology",
]
