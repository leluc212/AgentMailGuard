"""L3 Inbound Gate: the policy decision taken before any model call (ADR-0015 R-c)."""

from mailguard.layers.inbound_gate.gate import InboundGate

__all__ = ["InboundGate"]
