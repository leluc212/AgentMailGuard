"""Which endpoint served a model call: read it from the response, check it against the pin.

Sending the request header ``X-OpenRouter-Metadata: enabled`` makes an OpenRouter response
carry ``openrouter_metadata`` (the selected endpoint's provider, the attempt number, a
summary). The top-level ``provider`` field of the body is the fallback source of the served
provider; ``CallProvenance.provider_source`` records which field was used.
The generation id is the body ``id`` or the ``X-Generation-Id`` header. Nothing here
calls the network: ``parse_provenance`` reads a response the client already has, and
``check_pinned_route`` compares what it says with the routing the run pinned.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from packages.core.settings import ProviderRouting
from packages.llm.protocol import CallProvenance

METADATA_HEADER = "X-OpenRouter-Metadata"
METADATA_HEADER_VALUE = "enabled"
GENERATION_ID_HEADER = "X-Generation-Id"
MISMATCH_MARKER = "provider_mismatch"
SOURCE_ENDPOINTS = "openrouter_metadata.endpoints"
SOURCE_SUMMARY = "openrouter_metadata.summary"
SOURCE_BODY = "response.provider"

_SELECTED = re.compile(r"selected=([^,;]+)")
_NON_ALNUM = re.compile(r"[^a-z0-9]")


def _served_provider(
    data: Mapping[str, Any], metadata: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    """The served provider and the response field it came from, or ``(None, None)``.

    The router's metadata is read first (the selected endpoint entry, then its summary). When
    it names none, the top-level ``provider`` field OpenRouter puts on a chat-completion body is
    the fallback, so one unconfirmed metadata shape cannot make a whole pinned route
    unverifiable. Neither present: the call is unverified.
    """
    endpoints = metadata.get("endpoints")
    available = endpoints.get("available") if isinstance(endpoints, Mapping) else None
    if isinstance(available, list):
        for entry in available:
            if isinstance(entry, Mapping) and entry.get("selected") and entry.get("provider"):
                return str(entry["provider"]), SOURCE_ENDPOINTS
    summary = metadata.get("summary")
    if isinstance(summary, str):
        found = _SELECTED.search(summary)
        if found:
            return found.group(1).strip(), SOURCE_SUMMARY
    top_level = data.get("provider")
    if isinstance(top_level, str) and top_level.strip():
        return top_level.strip(), SOURCE_BODY
    return None, None


def parse_provenance(
    data: Mapping[str, Any], headers: Mapping[str, str], *, requested_model: str
) -> CallProvenance:
    """The provenance of one response; a field the response does not carry stays ``None``."""
    raw_meta = data.get("openrouter_metadata")
    metadata: Mapping[str, Any] = raw_meta if isinstance(raw_meta, Mapping) else {}
    attempt = metadata.get("attempt")
    raw_usage = data.get("usage")
    usage: Mapping[str, Any] = raw_usage if isinstance(raw_usage, Mapping) else {}
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


def _slug(name: str) -> str:
    """A provider name or tag as a comparable slug: ``coreweave/bf16`` becomes ``coreweave``."""
    return _NON_ALNUM.sub("", name.strip().lower().split("/")[0])


def check_pinned_route(provenance: Mapping[str, Any], routing: ProviderRouting) -> str | None:
    """Why a call does not honour the pinned route, or ``None`` when it does.

    Only a route with fallbacks off is a pin. The call must name its served provider (a response
    without the router's metadata cannot be verified, so it fails), that provider must be one of
    the pinned slugs, and the router must not have reported a fallback. Only the metadata numbers
    attempts, so an absent ``attempt`` is accepted (fallbacks are off and a pinned provider
    served the call); a reported one must be 1.
    """
    if routing.allow_fallbacks:
        return None
    served = provenance.get("served_provider")
    attempt = provenance.get("attempt")
    expected = {_slug(name) for name in routing.order}
    if not isinstance(served, str) or not served.strip():
        return "the response did not say which provider served it, so the pin cannot be verified"
    if _slug(served) not in expected:
        return f"served by {served!r}, pinned to {list(routing.order)}"
    if attempt is not None and attempt != 1:
        return f"served by {served!r} on attempt {attempt}, expected attempt 1 (a fallback ran)"
    return None
