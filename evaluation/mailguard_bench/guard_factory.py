"""Build AgentMailGuard pipelines for the benchmark without editing the guard branch.

Follows the guard's own harness (evaluation/harness.py:438 on
feature/mailguard-defense-stack): settings that ignore any .env, all four guard LLM stages
on one registered model, and a ModelRegistry that reads rag-email's guard_models.yaml by
absolute path. The L1 classifier path points at the artifact `make mailguard-prep` trained
outside the worktree. Needs `mailguard` importable (the `make mailguard-*` targets).
Task 7.19; ADR-0010.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from mailguard.config.settings import MailGuardSettings
from mailguard.llm.registry import ModelRegistry
from mailguard.pipeline import GuardConfig, MailGuardPipeline

from evaluation.mailguard_bench.guard_env import (
    GUARD_MODELS_YAML,
    PREP_HINT,
    GuardEnvError,
)

# --- guard v3 GuardConfig from the bench's v2 layer vocabulary (guard v3 Task 6) ------------
# The bench names layers l1, l2, l3, l3b, l4, l5 until scheme v3 (guard v3 Tasks 12-13). Guard
# v3 has one Inbound Guard switch for the old l1 (scan) and l2 (extract). Under R-i only the scan
# grades, so the Inbound Guard runs exactly when the set names l1 (with its extract sub-stage);
# a set with l2 but no l1 runs none. ``realized_layers`` reports what really ran, and the
# report's degradation check then flags a config that asked for one sub-stage only. The guard's
# parity matrix (agentmailguard/tests/parity.py, V2_CONFIG_FLAGS) uses the same rule.
V2_FULL_LAYERS = ("l1", "l2", "l3", "l3b", "l4", "l5")
V1_PRESET_LAYERS: dict[str, tuple[str, ...]] = {
    "C0": (),
    "C1": ("l1", "l5"),
    "C2": ("l1", "l2", "l3", "l5"),
    "C3": V2_FULL_LAYERS,
    **{
        f"C3-{layer.upper()}": tuple(x for x in V2_FULL_LAYERS if x != layer)
        for layer in V2_FULL_LAYERS
    },
}
"""The v2 guard presets the v1 scheme names, as v2 layer sets (``GuardConfig.preset`` is v3)."""


def v3_guard_config(name: str, layers: tuple[str, ...] | list[str]) -> GuardConfig:
    """The guard v3 ``GuardConfig`` for a v2 layer set (l5 switches the inbound gate; the
    outbound decision belongs to the Output Guard, l4, since guard v3 Task 8)."""
    wanted = set(layers)
    unknown = wanted - set(V2_FULL_LAYERS)
    if unknown:
        raise ValueError(f"unknown v2 layers {sorted(unknown)}")
    return GuardConfig(
        name,
        inbound_guard="l1" in wanted,
        doc_scanner="l3b" in wanted,
        inbound_gate="l5" in wanted,
        output_guard="l4" in wanted,
        channel_isolation="l3" in wanted,
    )


def v3_layers(layers: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """The v2-named layers guard v3 runs when the bench asks for ``layers`` (l1 → l1 + l2)."""
    return realized_layers(v3_guard_config("probe", layers))


def realized_layers(config: GuardConfig) -> tuple[str, ...]:
    """The v2-named layers a v3 ``GuardConfig`` really runs, in v2 order."""
    on = {
        "l1": config.inbound_guard,
        "l2": config.inbound_guard,
        "l3": config.channel_isolation,
        "l3b": config.doc_scanner,
        "l4": config.output_guard,
        "l5": config.inbound_gate,
    }
    return tuple(layer for layer in V2_FULL_LAYERS if on[layer])


def v1_preset_config(preset: str) -> GuardConfig:
    """``GuardConfig`` for a v1-scheme guard preset name (C0, C1, C2, C3, C3-L1 .. C3-L5)."""
    key = preset.upper()
    if key not in V1_PRESET_LAYERS:
        raise ValueError(f"unknown v1 guard preset {preset!r}")
    return v3_guard_config(key, V1_PRESET_LAYERS[key])


def guard_settings(
    model_name: str,
    *,
    l1_model_path: Path,
    models_path: Path = GUARD_MODELS_YAML,
    l3b_llm: bool = False,
    l4_llm: bool = False,
) -> MailGuardSettings:
    """Guard settings with every LLM stage on ``model_name``, independent of any .env.

    L3b/L4 LLM sub-stages stay off by default, as in the guard's own settings and harness.
    Absolute paths are used as given by MailGuardSettings.resolve.
    """
    settings = MailGuardSettings(_env_file=None)
    settings.guard_models.inbound_judge = model_name
    settings.guard_models.inbound_extractor = model_name
    settings.guard_models.doc_judge = model_name
    # Built only when doc_scanner.set_judge_llm_enabled is on (off by default, guard v3 Task 10).
    settings.guard_models.doc_set_judge = model_name
    settings.guard_models.output_judge = model_name
    settings.guard_models.models_path = str(models_path.resolve())
    settings.inbound_guard.ml_model_path = str(l1_model_path.resolve())
    settings.doc_scanner.llm_enabled = l3b_llm
    settings.output_guard.llm_enabled = l4_llm
    return settings


def build_guard_pipeline(preset: str, settings: MailGuardSettings) -> MailGuardPipeline:
    """A pipeline for one v1 guard preset (C0, C1, C2, C3, C3-L1..C3-L5), audit log off."""
    return MailGuardPipeline(
        settings, v1_preset_config(preset), registry=ModelRegistry(settings), audit=False
    )


@dataclass(frozen=True)
class LiveLayers:
    """Which guard parts are really live; recorded in the run manifest."""

    preset: str
    active_layers: tuple[str, ...]
    l1_classifier: bool
    l1_judge: str | None
    l2_llm: str | None
    l3b_llm: str | None
    l4_llm: str | None

    def as_dict(self) -> dict[str, object]:
        """JSON-ready form for manifest.json."""
        return asdict(self)


def provider_label(provider: object | None) -> str | None:
    """``<ProviderClass>:<model>`` for a guard LLM provider, None when the stage is off."""
    if provider is None:
        return None
    name = getattr(provider, "model", None) or getattr(provider, "model_name", None)
    return f"{type(provider).__name__}:{name}"


def live_layers(pipeline: MailGuardPipeline) -> LiveLayers:
    """Read what the pipeline actually built (the guard degrades silently otherwise)."""
    return LiveLayers(
        preset=str(pipeline.config.name),
        active_layers=realized_layers(pipeline.config),
        l1_classifier=bool(pipeline.inbound_guard.classifier.available),
        l1_judge=provider_label(pipeline.inbound_guard.judge),
        l2_llm=provider_label(pipeline.inbound_guard.extract.llm),
        l3b_llm=provider_label(pipeline.doc_scanner.llm),
        l4_llm=provider_label(pipeline.output_guard.llm),
    )


def require_live(
    layers: LiveLayers, *, model_name: str, l3b_llm: bool = False, l4_llm: bool = False
) -> None:
    """Fail unless the classifier is loaded and each wanted LLM stage runs ``model_name``.

    MailGuardPipeline turns a stage off with only a warning when its model is unknown, and a
    missing L1 artifact only logs a warning, so without this check a mis-set model name or
    a skipped prep step would report a weaker C3 than the real guard.

    Only the layers the preset runs are checked (``layers.active_layers``): C3-L1 has no
    L1 judge, C3-L2 no L2 LLM step, and the pipeline builds every layer's provider whether
    or not the preset runs the layer, so a stage of a removed layer is neither wanted nor
    "expected off". The classifier is needed wherever L1, L2 or L3b runs.
    """
    expected = f"OpenAIProvider:{model_name}"
    active = set(layers.active_layers)
    problems: list[str] = []
    if active & {"l1", "l2", "l3b"} and not layers.l1_classifier:
        problems.append(f"L1 classifier not loaded ({PREP_HINT})")
    wanted = {
        "l1_judge": ("l1", True),
        "l2_llm": ("l2", True),
        "l3b_llm": ("l3b", l3b_llm),
        "l4_llm": ("l4", l4_llm),
    }
    for field_name, (layer, on) in wanted.items():
        if layer not in active:
            continue
        got = getattr(layers, field_name)
        if on and got != expected:
            problems.append(f"{field_name} is {got}, expected {expected}")
        if not on and got is not None:
            problems.append(f"{field_name} is {got}, expected off")
    if problems:
        raise GuardEnvError("guard is not at full strength: " + "; ".join(problems))
