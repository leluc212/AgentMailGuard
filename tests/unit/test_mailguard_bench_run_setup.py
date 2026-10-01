"""What served a live run, as its report and summary state it (task 7.29; ADR-0014).

Pure functions over run metas: no model, no network, no docker (R24.5). The embedding is the
runner's choice, so each run states and records it, and a resume refuses another one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evaluation.mailguard_bench.artifacts import build_manifest
from evaluation.mailguard_bench.run_setup import (
    SECTION_TITLE,
    render_run_setup,
    run_setup,
    served_totals,
)
from evaluation.mailguard_bench.runner import RunSettingsMismatchError, check_resume
from evaluation.mailguard_bench.scoring import LIVE_TRANSPORT

PIN = {
    "order": ["coreweave"],
    "allow_fallbacks": False,
    "require_parameters": True,
    "quantizations": ["bf16"],
}
EMBEDDING = {
    "mock": False,
    "model": "text-embedding-3-small",
    "dimension": 1536,
    "base_url_host": "api.openai.com",
}


def _meta(*, routed: bool = True, provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    generation: dict[str, Any] = {
        "provider": "openai",
        "base_url": "https://openrouter.ai/api/v1" if routed else "https://api.openai.com/v1",
        "model": "meta-llama/llama-3.1-8b-instruct",
    }
    if routed:
        generation["provider_routing"] = PIN
        generation["response_metadata"] = True
    invocations: list[dict[str, Any]] = [{"summary": {}}]
    if provenance is not None:
        invocations.append({"summary": {"provenance": provenance}})
    return {
        "transport": LIVE_TRANSPORT,
        "generation_model": generation["model"],
        "generation": generation,
        "embedding": dict(EMBEDDING),
        "invocations": invocations,
    }


def _totals(calls: int, provider: str = "CoreWeave", cost: float = 0.01) -> dict[str, Any]:
    return {
        "calls": calls,
        "by_provider": {provider: calls},
        "fallback_attempts": 0,
        "unverified": 0,
        "cost_usd": cost,
    }


def test_an_in_process_v1_run_has_no_setup_section() -> None:
    meta = _meta()
    meta.pop("transport")
    assert run_setup({"C3": meta}) is None


def test_the_setup_names_the_model_route_and_embedding_without_a_key() -> None:
    setup = run_setup({"C0": _meta(), "C1": _meta(provenance=_totals(10))})
    assert setup is not None
    assert setup["generation_model"] == "meta-llama/llama-3.1-8b-instruct"
    assert setup["generation_host"] == "openrouter.ai"
    assert setup["provider_routing"] == PIN
    assert setup["embedding"] == EMBEDDING
    text = "\n".join(render_run_setup(setup))
    assert text.startswith(SECTION_TITLE)
    assert "`meta-llama/llama-3.1-8b-instruct` at `openrouter.ai`" in text
    assert "pinned to `coreweave` at bf16, fallbacks off" in text
    assert "not comparable with the v1 runs" in text and "4-bit" in text
    assert "`text-embedding-3-small` at `api.openai.com`, 1536 dimensions" in text
    assert "same embedding model" in text


def test_the_served_providers_are_summed_over_configs_from_each_last_invocation() -> None:
    first = _meta(provenance=_totals(4, cost=0.5))
    first["invocations"].append({"summary": {"provenance": _totals(10, cost=1.0)}})  # cumulative
    totals = served_totals({"C1": first, "C2": _meta(provenance=_totals(5, cost=0.25))})
    assert totals == {
        "calls": 15,
        "by_provider": {"CoreWeave": 15},
        "fallback_attempts": 0,
        "unverified": 0,
        "cost_usd": 1.25,
    }
    text = "\n".join(
        render_run_setup(run_setup({"C1": first, "C2": _meta(provenance=_totals(5))}) or {})
    )
    assert "`CoreWeave` 15" in text and "0 after a fallback, 0 unverified" in text


def test_an_unrouted_run_says_so_and_still_names_its_embedding() -> None:
    text = "\n".join(render_run_setup(run_setup({"C0": _meta(routed=False)}) or {}))
    assert "Route: direct; no provider pin" in text
    assert "`api.openai.com`" in text and "`text-embedding-3-small`" in text
    assert "not comparable" not in text


def test_the_fake_embedder_is_named_as_such() -> None:
    meta = _meta(routed=False)
    meta["embedding"] = {"mock": True, "model": "x", "dimension": 1536, "base_url_host": None}
    text = "\n".join(render_run_setup(run_setup({"C0": meta}) or {}))
    assert "the fake embedder" in text


def test_the_manifest_carries_each_configs_embedding() -> None:
    models = {"C0": {"generation": "m", "embedding": EMBEDDING}}
    manifest = build_manifest(
        run_id="r",
        rag_email={"sha": "a"},
        mailguard={"sha": "b"},
        case_manifest_sha256=None,
        run_meta={"C0": _meta()},
        models=models,
        counts={},
    )
    assert manifest["models"]["C0"]["embedding"]["model"] == "text-embedding-3-small"
    assert manifest["runs"]["C0"]["embedding"]["base_url_host"] == "api.openai.com"


@pytest.mark.parametrize(
    ("field", "other"),
    [("model", "gemini-embedding-001"), ("base_url_host", "generativelanguage.googleapis.com")],
)
def test_a_resume_under_another_embedding_is_refused(
    tmp_path: Path, field: str, other: str
) -> None:
    fingerprint = {"scheme": "v2", "generation_model": "m", "embedding": dict(EMBEDDING)}
    meta_file = tmp_path / "C0.meta.json"
    meta_file.write_text(
        json.dumps({"fingerprint": fingerprint, "invocations": [{"started": "t"}]}),
        encoding="utf-8",
    )
    assert check_resume(meta_file, fingerprint) == [{"started": "t"}]
    with pytest.raises(RunSettingsMismatchError, match="embedding changed"):
        check_resume(meta_file, {**fingerprint, "embedding": {**EMBEDDING, field: other}})
