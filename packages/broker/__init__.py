"""Broker messaging foundation package (R3.1, R3.2, R3.3, R3.4, R3.5, R3.8).

Exports topology declaration, standard job envelopes, persistent publishers,
and base consumer abstractions.
"""

from packages.broker.consumer import BaseConsumer, FatalError, TransientError
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
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
    "BaseConsumer",
    "BrokerTopology",
    "CANONICAL_LANES",
    "FatalError",
    "HIGH_PRIORITY_LEVELS",
    "JobEnvelope",
    "MessagePublisher",
    "TransientError",
    "format_routing_key",
    "is_queue_consumed",
    "load_categories_from_yaml",
    "prepare_route_envelope",
    "resolve_priority_lane",
    "setup_topology",
]
