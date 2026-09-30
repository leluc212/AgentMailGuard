"""Read which endpoint served a call from an OpenRouter chat-completion response.

Sending the request header ``X-OpenRouter-Metadata: enabled`` makes the response carry
``openrouter_metadata`` (the selected endpoint's provider, the attempt number and a summary).
The generation id is the body ``id`` or the ``X-Generation-Id`` response header.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from mailguard.llm.protocol import CallProvenance

METADATA_HEADER = "X-OpenRouter-Metadata"
METADATA_HEADER_VALUE = "enabled"
GENERATION_ID_HEADER = "X-Generation-Id"

# The keys of OpenRouter's ProviderPreferences object (its schema has additionalProperties
# false, so a key outside it is refused by the API): send only what it documents.
PROVIDER_ROUTING_KEYS = frozenset(
    {
        "order",
        "allow_fallbacks",
        "require_parameters",
        "data_collection",
        "zdr",
        "enforce_distillable_text",
        "only",
        "ignore",
        "quantizations",
        "sort",
        "preferred_min_throughput",
        "preferred_max_latency",
        "max_price",
    }
)

_SELECTED = re.compile(r"selected=([^,;]+)")


def validate_provider_routing(routing: Mapping[str, Any]) -> dict[str, Any]:
    """The routing object as a plain dict, or a ValueError naming a key OpenRouter does not have."""
    unknown = sorted(set(routing) - PROVIDER_ROUTING_KEYS)
    if unknown:
        raise ValueError(f"unsupported provider routing key(s): {', '.join(unknown)}")
    return dict(routing)


def _served_provider(
    data: Mapping[str, Any], metadata: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    """The served provider and which field it came from, or ``(None, None)``.

    The router's metadata is read first (endpoint entry, then its summary). Without it, the
    top-level ``provider`` field OpenRouter puts on a chat-completion body is the fallback, so
    one unconfirmed shape does not make a pinned route unverifiable.
    """
    endpoints = metadata.get("endpoints")
    available = endpoints.get("available") if isinstance(endpoints, Mapping) else None
    if isinstance(available, list):
        for entry in available:
            if isinstance(entry, Mapping) and entry.get("selected") and entry.get("provider"):
                return str(entry["provider"]), "openrouter_metadata.endpoints"
    summary = metadata.get("summary")
    if isinstance(summary, str):
        found = _SELECTED.search(summary)
        if found:
            return found.group(1).strip(), "openrouter_metadata.summary"
    top_level = data.get("provider")
    if isinstance(top_level, str) and top_level.strip():
        return top_level.strip(), "response.provider"
    return None, None


def parse_provenance(
    data: Mapping[str, Any], headers: Mapping[str, str], *, requested_model: str
) -> CallProvenance:
    """The provenance of one response; fields the response does not carry stay ``None``."""
    raw_meta = data.get("openrouter_metadata")
    metadata: Mapping[str, Any] = raw_meta if isinstance(raw_meta, Mapping) else {}
    attempt = metadata.get("attempt")
    usage = data.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    cost = usage.get("cost")
    choices = data.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else {}
    finish = first.get("finish_reason") if isinstance(first, Mapping) else None
    generation_id = data.get("id") or headers.get(GENERATION_ID_HEADER)
    summary = metadata.get("summary")
    served, source = _served_provider(data, metadata)
    return CallProvenance(
        requested_model=requested_model,
        served_provider=served,
        provider_source=source,
        attempt=attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else None,
        summary=summary if isinstance(summary, str) else None,
        generation_id=str(generation_id) if generation_id else None,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        cost=float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None,
        finish_reason=str(finish) if finish else None,
    )
