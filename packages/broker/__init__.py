"""Broker messaging foundation package (R3.1, R3.2, R3.3, R3.4, R3.5, R3.8).

Exports topology declaration, standard job envelopes, persistent publishers,
and base consumer abstractions.
"""

from packages.broker.consumer import BaseConsumer, FatalError, TransientError
from packages.broker.envelope import JobEnvelope
from packages.broker.publisher import MessagePublisher
from packages.broker.topology import BrokerTopology, setup_topology

__all__ = [
    "BaseConsumer",
    "BrokerTopology",
    "FatalError",
    "JobEnvelope",
    "MessagePublisher",
    "TransientError",
    "setup_topology",
]
