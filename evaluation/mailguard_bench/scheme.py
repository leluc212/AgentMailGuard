"""The benchmark's config scheme: what the names C0..C7 mean (task 7.20; ADR-0012 decision 11).

Two schemes use the same config names for different things. A run records its scheme in every
meta and in its settings fingerprint, and a run folder never mixes them.

    v1  the published benchmark: C0 native, C0T guard template with no layer, C1 = L1+L5,
        C2 = L1+L2+L3+L5, C3 = every layer, and the remove-one ablation C3-L1 .. C3-L5
        (the guard's own presets, on the pinned guard 81df5d07)
    v2  the 2026-09-30 configs (owner decision 23:10), each built from explicit layer flags:

        C0   no guard (rag-email's own prompt)        C4  L3b document scanner + L5
        C0T  the guard's prompt template, no layer    C5  L4 output scanner + L5
        C1   L1 inbound scanner + L5                  C6  L5 policy engine alone (a control)
        C2   L2 intent extractor + L5                 C7  the full guard, all layers
        C3   L3 channel isolation + L5

A meta without a ``scheme`` key was written before the schemes existed and is v1. New runs are v2
unless they ask for v1. Nothing here imports AgentMailGuard, so CI covers it.

(docs/adr/0012-post-review-v2-done-fixes-and-main.md decision 11;
docs/superpowers/specs/2026-09-29-mailguard-live-v2-design.md Amendment 2; R22.12)
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEME_KEY = "scheme"
SCHEME_V1 = "v1"
SCHEME_V2 = "v2"
SCHEMES = (SCHEME_V1, SCHEME_V2)
DEFAULT_SCHEME = SCHEME_V2
"""A new run's scheme unless it asks for v1."""

FULL_LAYERS = ("l1", "l2", "l3", "l3b", "l4", "l5")

# --- v1: the published names ---------------------------------------------------------------
V1_CONFIGS = ("C0", "C0T", "C1", "C2", "C3")
# The layer each ablation config removes from the full guard (GuardConfig.preset "C3-<L>").
LAYER_ABLATIONS: dict[str, str] = {
    "C3-L1": "l1",
    "C3-L2": "l2",
    "C3-L3": "l3",
    "C3-L3B": "l3b",
    "C3-L4": "l4",
    "C3-L5": "l5",
}
ABLATION_CONFIGS = tuple(LAYER_ABLATIONS)
V1_TARGET_CONFIG = "C3"

# --- v2: explicit layer flags --------------------------------------------------------------
V2_LAYERS: dict[str, tuple[str, ...]] = {
    "C0": (),  # rag-email's own path: no guard code runs at all
    "C0T": (),  # the guard's template, no layer active
    "C1": ("l1", "l5"),
    "C2": ("l2", "l5"),
    "C3": ("l3", "l5"),
    "C4": ("l3b", "l5"),
    "C5": ("l4", "l5"),
    "C6": ("l5",),
    "C7": FULL_LAYERS,
}
V2_CONFIGS = tuple(V2_LAYERS)
V2_TARGET_CONFIG = "C7"
V2_AI_STAGES: dict[str, tuple[str, ...]] = {
    "C0": (),
    "C0T": (),
    "C1": ("l1.judge",),
    "C2": ("l2.llm",),
    "C3": (),
    "C4": ("l3b.llm",),
    "C5": ("l4.llm",),
    "C6": (),
    "C7": ("l1.judge", "l2.llm", "l3b.llm", "l4.llm"),
}
"""The guard AI stage that is live in each v2 config (the owner's list, 2026-09-30)."""
STAGE_LAYER_NAMES = {
    "l1.judge": "l1_injection_scanner",
    "l2.llm": "l2_intent_extractor",
    "l3b.llm": "l3b_document_scanner",
    "l4.llm": "l4_output_scanner",
}
CLASSIFIER_LAYERS = frozenset({"l1", "l2", "l3b"})
"""The layers that read the trained L1 classifier (the pipeline hands L2 and L3b L1's)."""


class SchemeMixError(ValueError):
    """A run folder would hold runs of two config schemes."""


def configs_for(scheme: str) -> tuple[str, ...]:
    """Every config name that means something in ``scheme``, in report order.

    Raises:
        ValueError: If ``scheme`` is neither v1 nor v2.
    """
    if scheme == SCHEME_V1:
        return (*V1_CONFIGS, *ABLATION_CONFIGS)
    if scheme == SCHEME_V2:
        return V2_CONFIGS
    raise ValueError(f"unknown config scheme {scheme!r}; expected one of {SCHEMES}")


def require_config(scheme: str, config: str) -> None:
    """Fail unless ``config`` is one of ``scheme``'s configs.

    Raises:
        ValueError: Naming the scheme and its configs.
    """
    known = configs_for(scheme)
    if config not in known:
        raise ValueError(
            f"config {config!r} does not exist in scheme {scheme}; its configs are "
            f"{', '.join(known)}"
        )


def target_config(scheme: str) -> str:
    """The config whose guard ASR the target line is judged on: the full guard."""
    configs_for(scheme)
    return V2_TARGET_CONFIG if scheme == SCHEME_V2 else V1_TARGET_CONFIG


def v2_guard_name(config: str) -> str:
    """``GuardConfig.name`` of a guarded v2 config: its own, never a guard preset's name.

    The guard has presets called C0 to C3 with other meanings; the name says these flags are v2's.

    Raises:
        ValueError: If ``config`` is the native path (no guard) or not a v2 config.
    """
    if config == "C0":
        raise ValueError("C0 is rag-email's native path and has no guard config")
    require_config(SCHEME_V2, config)
    return f"v2-{config}"


def v2_llm_stages(config: str) -> tuple[bool, bool]:
    """``(l3b_llm, l4_llm)``: the optional guard LLM stages a v2 config runs.

    L1's judge and L2's extraction are always built; L3b's and L4's AI stages are opt-in, and a
    v2 config turns one on exactly when the config lists it.
    """
    require_config(SCHEME_V2, config)
    stages = V2_AI_STAGES[config]
    return "l3b.llm" in stages, "l4.llm" in stages


def v2_required_stages(config: str) -> tuple[str, ...]:
    """The guard stages that must be live for a v2 config: the classifier, then its AI stages."""
    require_config(SCHEME_V2, config)
    needs_classifier = bool(CLASSIFIER_LAYERS & set(V2_LAYERS[config]))
    return (*(("l1.classifier",) if needs_classifier else ()), *V2_AI_STAGES[config])


def v2_ai_layer_names(config: str) -> tuple[str, ...]:
    """The guard layers (by ``LayerName`` value) whose AI step a v2 config runs."""
    require_config(SCHEME_V2, config)
    return tuple(STAGE_LAYER_NAMES[stage] for stage in V2_AI_STAGES[config])


def scheme_of_meta(meta: Mapping[str, Any] | None) -> str:
    """The scheme a run meta records; a meta without one is v1.

    Raises:
        ValueError: If the meta names a scheme that does not exist.
    """
    recorded = (meta or {}).get(SCHEME_KEY)
    if recorded is None:
        return SCHEME_V1
    if recorded not in SCHEMES:
        raise ValueError(f"unknown config scheme {recorded!r} in a run meta; expected {SCHEMES}")
    return str(recorded)


def _folder_schemes(run_dir: Path) -> dict[str, str]:
    """Scheme by meta file name for every readable ``raw/*.meta.json`` of ``run_dir``.

    A guard-worker's meta counts, so a folder whose worker started under one scheme is that
    scheme's. A file that does not parse (a write torn by a crash) says nothing.
    """
    found: dict[str, str] = {}
    for path in sorted((run_dir / "raw").glob("*.meta.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict):
            found[path.name] = scheme_of_meta(meta)
    return found


def folder_scheme(run_dir: Path) -> str | None:
    """The scheme every meta of ``run_dir`` records, or None when it has no meta yet.

    Raises:
        SchemeMixError: If its metas record different schemes.
    """
    found = _folder_schemes(run_dir)
    kinds = set(found.values())
    if len(kinds) > 1:
        detail = "; ".join(f"{name}: {kind}" for name, kind in found.items())
        raise SchemeMixError(
            f"{run_dir} mixes config schemes ({detail}); a run folder holds one scheme, so v1 "
            "and v2 numbers are never compared. Move one scheme to a new RUN"
        )
    return next(iter(kinds), None)


def require_folder_scheme(run_dir: Path, scheme: str) -> None:
    """Refuse to add a ``scheme`` run to a folder that already holds the other scheme.

    Raises:
        SchemeMixError: Naming the files that record the other scheme.
    """
    configs_for(scheme)
    found = _folder_schemes(run_dir)
    other = {name: kind for name, kind in found.items() if kind != scheme}
    if other:
        files = ", ".join(f"{name} ({kind})" for name, kind in other.items())
        raise SchemeMixError(
            f"{run_dir} already holds {sorted(set(other.values()))[0]} runs ({files}) and this "
            f"run is scheme {scheme}: a run folder never mixes config schemes. Use a new RUN, or "
            f"pass --scheme {sorted(set(other.values()))[0]} to continue that one"
        )
