"""What served a live run, read back from its metas: the model, its route, the embedding.

Task 7.29; ADR-0014. A v2 run folder holds one model's configs, and the report compares those
configs with each other only (``report.SHARED_SETTINGS`` refuses a folder whose configs differ in
the generation or the embedding settings). The comparison across models is made between run
folders, by a reader, so each run's report and ``summary.json`` state what that comparison must
hold equal or call out:

- the model under test and its endpoint's host;
- for an OpenRouter run, the pinned provider and the providers that actually served the calls the
  rows record, and that its numbers are not comparable with the v1 local 4-bit runs on serving;
- the embedding model, its endpoint's host and width. It is the runner's choice and must be the
  same for every model of a comparison: a different embedding changes what retrieval finds.

No key is read or written: a meta records the host of each base URL, never a key.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from evaluation.mailguard_bench.scoring import LIVE_TRANSPORT

SECTION_TITLE = "## Run setup"


def _host(url: object) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    return urlsplit(url).hostname


def _latest_provenance(meta: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The provenance totals of the config's last invocation that recorded any.

    Each invocation's totals cover every row recorded so far (``runner.add_route_summary``), so
    the last one is the config's.
    """
    for invocation in reversed(meta.get("invocations") or []):
        if not isinstance(invocation, Mapping):
            continue
        totals = (invocation.get("summary") or {}).get("provenance")
        if isinstance(totals, Mapping):
            return totals
    return None


def served_totals(run_meta: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    """The provenance totals summed over the configs; None when no config recorded any."""
    by_provider: Counter[str] = Counter()
    calls = fallback_attempts = unverified = 0
    cost = 0.0
    found = False
    for meta in run_meta.values():
        totals = _latest_provenance(meta)
        if totals is None:
            continue
        found = True
        calls += int(totals.get("calls") or 0)
        fallback_attempts += int(totals.get("fallback_attempts") or 0)
        unverified += int(totals.get("unverified") or 0)
        cost += float(totals.get("cost_usd") or 0.0)
        for provider, count in (totals.get("by_provider") or {}).items():
            by_provider[str(provider)] += int(count)
    if not found:
        return None
    return {
        "calls": calls,
        "by_provider": dict(sorted(by_provider.items())),
        "fallback_attempts": fallback_attempts,
        "unverified": unverified,
        "cost_usd": cost,
    }


def run_setup(run_meta: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    """The setup facts of a live run; None for a run of the in-process harness (v1).

    The configs of one folder share the generation and embedding settings (the report refuses
    them otherwise), so the first meta speaks for the run; the served-provider totals are summed
    over every config.
    """
    metas = [meta for meta in run_meta.values() if meta]
    if not metas or not any(meta.get("transport") == LIVE_TRANSPORT for meta in metas):
        return None
    meta = metas[0]
    generation = meta.get("generation") or {}
    embedding = meta.get("embedding") or {}
    return {
        "generation_model": meta.get("generation_model"),
        "generation_host": _host(generation.get("base_url")),
        "provider_routing": generation.get("provider_routing"),
        "served": served_totals(run_meta),
        "embedding": {
            "mock": embedding.get("mock", meta.get("embedding_mock")),
            "model": embedding.get("model"),
            "dimension": embedding.get("dimension"),
            "base_url_host": embedding.get("base_url_host"),
        },
    }


def _route_line(routing: Mapping[str, Any], served: Mapping[str, Any] | None) -> str:
    order = ", ".join(f"`{slug}`" for slug in routing.get("order") or [])
    precision = "/".join(str(q) for q in routing.get("quantizations") or [])
    pin = f"pinned to {order}" + (f" at {precision}" if precision else ", precision not filtered")
    fallbacks = "fallbacks off" if routing.get("allow_fallbacks") is False else "fallbacks ON"
    line = f"- Route: OpenRouter, {pin}, {fallbacks}."
    if served is None:
        line += " No row records a served provider."
    else:
        providers = ", ".join(
            f"`{name}` {count}" for name, count in (served.get("by_provider") or {}).items()
        )
        line += (
            f" Calls the rows record (generation and the guard's judges): {served['calls']}, "
            f"served by {providers or 'none'}; {served['fallback_attempts']} after a fallback, "
            f"{served['unverified']} unverified; ${served['cost_usd']:.4f} as the router "
            "reported it. Triage, the summarizer and C0's calls are verified against the pin "
            "too (a mismatch is an error row) and logged by the services (`llm_inference`)."
        )
    return (
        line + " Its numbers are not comparable with the v1 runs on serving: those ran "
        "Qwen2.5-7B and Llama-3.1-8B locally in 4-bit builds on Ollama (ADR-0014)."
    )


def render_run_setup(setup: Mapping[str, Any]) -> list[str]:
    """``## Run setup``: the model, its route and the embedding, for a reader comparing runs."""
    host = setup.get("generation_host") or "unknown host"
    lines = [
        SECTION_TITLE,
        "",
        f"- Model under test: `{setup.get('generation_model') or 'unknown'}` at `{host}`, in "
        "every LLM role of both systems (triage stage 3, the summarizer, the reply and its "
        "repair, and the guard's judges).",
    ]
    routing = setup.get("provider_routing")
    if isinstance(routing, Mapping) and routing:
        lines.append(_route_line(routing, setup.get("served")))
    else:
        lines.append("- Route: direct; no provider pin (the endpoint serves the model itself).")
    embedding = setup.get("embedding") or {}
    if embedding.get("mock"):
        lines.append("- Embedding: the fake embedder; retrieval was not semantic.")
    else:
        lines.append(
            f"- Embedding: `{embedding.get('model') or 'unknown'}` at "
            f"`{embedding.get('base_url_host') or 'unknown host'}`, "
            f"{embedding.get('dimension') or 'unknown'} dimensions, the runner's choice "
            "(ADR-0014). Compare this run only with runs that used the same embedding model: "
            "it decides what retrieval finds."
        )
    return lines
