"""The benchmark's config scheme: what the config names mean (task 7.20; ADR-0012 decision 11).

Three schemes use the same config names for different things. A run records its scheme in every
meta and in its settings fingerprint, and a run folder never mixes them.

    v3  the guard v3 configs (ADR-0015, plan_v3 Task 12), the only scheme a run at HEAD takes.
        The names are the guard's own presets (``GuardConfig.preset``), plus the two baselines:

        C0     no guard (rag-email's own prompt)      C3-L1  C3 without the L1 Inbound Guard
        C0T    the guard's prompt template, no layer  C3-L2  C3 without the L2 Doc Scanner
        C1     L1 Inbound Guard + L3 Inbound Gate     C3-L3  C3 without the L3 Inbound Gate
        C2     C1 + L2 Doc Scanner + isolation        C3-L4  C3 without the L4 Output Guard
        C3     the full guard                         C3-ISO C3 without channel isolation

        Every judge of a layer the config runs is live (``V3_AI_STAGES``). A judge that fails
        or answers outside its schema fails closed (``MAILGUARD__FAIL_CLOSED_ON_JUDGE_ERROR``)
        unless the run asks for the v2 fallback (``--judge-fallback``, to compare with v2).

Schemes v1 and v2 stay readable (their committed run folders), but they ran on other guard
commits: a run of either at HEAD is refused (``require_run_scheme``), naming the commit to use.

    v1  the published benchmark: C0 native, C0T guard template with no layer, C1 = L1+L5,
        C2 = L1+L2+L3+L5, C3 = every layer, and the remove-one ablation C3-L1 .. C3-L5
        (the guard's own presets, on the pinned guard 81df5d07)
    v2  the 2026-09-30 configs (owner decision 23:10), each built from explicit layer flags:

        C0   no guard (rag-email's own prompt)        C4  L3b document scanner + L5
        C0T  the guard's prompt template, no layer    C5  L4 output scanner + L5
        C1   L1 inbound scanner + L5                  C6  L5 policy engine alone (a control)
        C2   L2 intent extractor + L5                 C7  the full guard, all layers
        C3   L3 channel isolation + L5

A meta without a ``scheme`` key was written before the schemes existed and is v1. New runs are v3.
Nothing here imports AgentMailGuard, so CI covers it.

(docs/adr/0012-post-review-v2-done-fixes-and-main.md decision 11;
docs/superpowers/specs/2026-09-29-mailguard-live-v2-design.md Amendment 2; R22.12)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEME_KEY = "scheme"
SCHEME_V1 = "v1"
SCHEME_V2 = "v2"
SCHEME_V3 = "v3"
SCHEMES = (SCHEME_V1, SCHEME_V2, SCHEME_V3)
"""Every scheme a run meta may record (the committed v1/v2 folders stay readable)."""
RUN_SCHEMES = (SCHEME_V3,)
"""The schemes a run at HEAD takes."""
DEFAULT_SCHEME = SCHEME_V3
"""A new run's scheme."""
RETIRED_SCHEME_GUARDS: dict[str, str] = {
    SCHEME_V1: "81df5d07b15b5bb3d1ecf3aae556df01e304cbe0",
    SCHEME_V2: "915cb1e2b86395e4389673deb9914308cc39627b",
}
"""The guard commit each retired scheme ran on (``guard_env.V1_/V2_MAILGUARD_COMMIT``)."""
JUDGE_FAIL_CLOSED_KEY = "judge_fail_closed"
"""Run meta and fingerprint key: whether a failed judge answer failed closed (v3 default)."""

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

# --- v3: the guard v3 presets (ADR-0015) ---------------------------------------------------
V3_SWITCHES = ("inbound_guard", "doc_scanner", "inbound_gate", "output_guard", "channel_isolation")
"""``GuardConfig``'s switches: the four layers, then channel isolation (not a layer, R-d)."""
_V3_FULL = V3_SWITCHES
V3_LAYERS: dict[str, tuple[str, ...]] = {
    "C0": (),  # rag-email's own path: no guard code runs at all
    "C0T": (),  # the guard's template, nothing active (GuardConfig.preset("C0"))
    "C1": ("inbound_guard", "inbound_gate"),
    "C2": ("inbound_guard", "doc_scanner", "inbound_gate", "channel_isolation"),
    "C3": _V3_FULL,
    "C3-L1": tuple(s for s in _V3_FULL if s != "inbound_guard"),
    "C3-L2": tuple(s for s in _V3_FULL if s != "doc_scanner"),
    "C3-L3": tuple(s for s in _V3_FULL if s != "inbound_gate"),
    "C3-L4": tuple(s for s in _V3_FULL if s != "output_guard"),
    "C3-ISO": tuple(s for s in _V3_FULL if s != "channel_isolation"),
}
"""The switches each v3 config turns on: exactly ``GuardConfig.preset`` of its guard preset."""
V3_CONFIGS = tuple(V3_LAYERS)
V3_TARGET_CONFIG = "C3"
V3_ABLATIONS: dict[str, str] = {
    "C3-L1": "inbound_guard",
    "C3-L2": "doc_scanner",
    "C3-L3": "inbound_gate",
    "C3-L4": "output_guard",
    "C3-ISO": "channel_isolation",
}
"""The switch each v3 ablation turns off (the guard's ``GuardConfig.ABLATIONS``)."""
V3_DEFAULT_CAMPAIGN = tuple(c for c in V3_CONFIGS if c != "C0T")
"""The configs of the v3 live benchmark (plan_v3 Task 16); C0T runs when asked for."""
V3_LAYER_JUDGES: dict[str, tuple[str, ...]] = {
    "inbound_guard": ("inbound_judge", "inbound_extractor"),
    "doc_scanner": ("doc_judge", "doc_set_judge"),
    "output_guard": ("output_judge",),
}
"""The guard judges each layer owns (ADR-0015 decision 12; at most five calls per job)."""
V3_STAGES = ("classifier", *(j for judges in V3_LAYER_JUDGES.values() for j in judges))
"""Every live-checked guard stage of v3: the trained classifier, then the five judges."""
V3_CLASSIFIER_LAYERS = frozenset({"inbound_guard", "doc_scanner"})
"""The layers that read the trained classifier (the Doc Scanner gets the Inbound Guard's)."""
V3_AI_STAGES: dict[str, tuple[str, ...]] = {
    config: tuple(
        judge for layer in V3_LAYER_JUDGES if layer in on for judge in V3_LAYER_JUDGES[layer]
    )
    for config, on in V3_LAYERS.items()
}
"""Every judge of every layer a v3 config runs is live (the full architecture is measured)."""
V3_JUDGE_STAGE: dict[str, str] = {
    "inbound_judge": "inbound_scan",
    "inbound_extractor": "inbound_extract",
    "doc_judge": "doc_chunk",
    "doc_set_judge": "doc_set",
    "output_judge": "output",
}
"""The guard's own name (``JudgeStage``, the keys of ``report.metadata["guard_calls"]`` and of a
verdict's ``judge_invalid``) of each judge ``V3_LAYER_JUDGES`` lists. The reports speak in these."""
GUARD_STAGES = tuple(V3_JUDGE_STAGE.values())
"""Every guard judge stage, in pipeline order (``mailguard.llm.judge_calls.JUDGE_STAGES``)."""


class SchemeMixError(ValueError):
    """A run folder would hold runs of two config schemes."""


def configs_for(scheme: str) -> tuple[str, ...]:
    """Every config name that means something in ``scheme``, in report order.

    Raises:
        ValueError: If ``scheme`` is not one of ``SCHEMES``.
    """
    if scheme == SCHEME_V1:
        return (*V1_CONFIGS, *ABLATION_CONFIGS)
    if scheme == SCHEME_V2:
        return V2_CONFIGS
    if scheme == SCHEME_V3:
        return V3_CONFIGS
    raise ValueError(f"unknown config scheme {scheme!r}; expected one of {SCHEMES}")


def require_run_scheme(scheme: str) -> None:
    """Fail unless a run at HEAD may take ``scheme`` (v3 only; ADR-0015 Consequences).

    Raises:
        ValueError: For v1 and v2, naming the guard commit their runs need.
    """
    configs_for(scheme)
    if scheme in RETIRED_SCHEME_GUARDS:
        raise ValueError(
            f"scheme {scheme} ran on guard {RETIRED_SCHEME_GUARDS[scheme]}; this checkout runs "
            f"the v3 guard and takes scheme {SCHEME_V3} only. Run scheme {scheme} from a checkout "
            f"of that commit (its committed folders stay readable here)"
        )


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


def live_configs_for(scheme: str) -> tuple[str, ...]:
    """The configs a live run (live runner, guard-worker) takes in ``scheme``.

    v1's remove-one ablation (C3-L1 .. C3-L5) is in-process only: its presets switch L3b's and
    L4's AI stages off, so a live one would be a weaker guard than live C3 and was never designed.
    v3's ablations run live with every judge of the layers they keep (``V3_AI_STAGES``).

    Raises:
        ValueError: If ``scheme`` is not one of ``SCHEMES``.
    """
    return V1_CONFIGS if scheme == SCHEME_V1 else configs_for(scheme)


def require_live_config(scheme: str, config: str) -> None:
    """Fail unless a live run of ``scheme`` takes ``config``.

    Raises:
        ValueError: Naming the scheme and its live configs.
    """
    known = live_configs_for(scheme)
    if config not in known:
        raise ValueError(
            f"config {config!r} cannot run live in scheme {scheme}; its live configs are "
            f"{', '.join(known)}"
        )


def target_config(scheme: str) -> str:
    """The config whose guard ASR the target line is judged on: the full guard."""
    configs_for(scheme)
    return {SCHEME_V2: V2_TARGET_CONFIG, SCHEME_V3: V3_TARGET_CONFIG}.get(scheme, V1_TARGET_CONFIG)


def v3_guard_preset(config: str) -> str:
    """The ``GuardConfig.preset`` a guarded v3 config builds: its own name, C0T the empty ``C0``.

    Raises:
        ValueError: If ``config`` is the native path (no guard) or not a v3 config.
    """
    if config == "C0":
        raise ValueError("C0 is rag-email's native path and has no guard config")
    require_config(SCHEME_V3, config)
    return "C0" if config == "C0T" else config


def v3_llm_stages(config: str) -> tuple[bool, bool]:
    """``(doc_llm, output_llm)``: the opt-in guard judges a v3 config runs.

    ``doc_llm`` is the Doc Scanner's chunk judge and set judge, ``output_llm`` the Output Guard's
    judge (both off in the guard's own defaults); the Inbound Guard's two judges are always built.
    A config turns them on exactly when it runs the layer.
    """
    require_config(SCHEME_V3, config)
    stages = V3_AI_STAGES[config]
    return "doc_judge" in stages, "output_judge" in stages


def v3_required_stages(config: str) -> tuple[str, ...]:
    """The guard stages that must be live for a v3 config: the classifier, then its judges."""
    require_config(SCHEME_V3, config)
    needs_classifier = bool(V3_CLASSIFIER_LAYERS & set(V3_LAYERS[config]))
    return (*(("classifier",) if needs_classifier else ()), *V3_AI_STAGES[config])


def v3_ai_stage_names(config: str) -> tuple[str, ...]:
    """The guard judge stages (``GUARD_STAGES`` names) whose AI step a v3 config runs."""
    require_config(SCHEME_V3, config)
    return tuple(V3_JUDGE_STAGE[judge] for judge in V3_AI_STAGES[config])


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
            f"{run_dir} mixes config schemes ({detail}); a run folder holds one scheme, so the "
            "numbers of two schemes are never compared. Move one scheme to a new RUN"
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
        held = sorted(set(other.values()))[0]
        hint = (
            f"pass --scheme {held} to continue that one"
            if held in RUN_SCHEMES
            else f"scheme {held} runs only from a checkout of guard {RETIRED_SCHEME_GUARDS[held]}"
        )
        raise SchemeMixError(
            f"{run_dir} already holds {held} runs ({files}) and this run is scheme {scheme}: a "
            f"run folder never mixes config schemes. Use a new RUN ({hint})"
        )


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m evaluation.mailguard_bench.scheme --run-dir DIR``: print the folder's scheme.

    Prints ``v1``, ``v2``, ``v3`` or ``none`` (no meta yet) and returns 0; a folder that mixes the
    schemes prints ``FAIL ...`` on stderr and returns 1. The Make targets branch on it.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        found = folder_scheme(args.run_dir)
    except SchemeMixError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print(found or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
