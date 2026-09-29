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
    settings.guard_models.judge = model_name
    settings.guard_models.extractor = model_name
    settings.guard_models.doc_scanner = model_name
    settings.guard_models.output_judge = model_name
    settings.guard_models.models_path = str(models_path.resolve())
    settings.l1.ml_model_path = str(l1_model_path.resolve())
    settings.l3b.llm_enabled = l3b_llm
    settings.l4.llm_enabled = l4_llm
    return settings


def build_guard_pipeline(preset: str, settings: MailGuardSettings) -> MailGuardPipeline:
    """A pipeline for one GuardConfig preset (C0, C1, C2, C3), audit log off."""
    return MailGuardPipeline(
        settings, GuardConfig.preset(preset), registry=ModelRegistry(settings), audit=False
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
        active_layers=tuple(pipeline.config.active_layers),
        l1_classifier=bool(pipeline.l1.classifier.available),
        l1_judge=provider_label(pipeline.l1.judge),
        l2_llm=provider_label(pipeline.l2.llm),
        l3b_llm=provider_label(pipeline.l3b.llm),
        l4_llm=provider_label(pipeline.l4.llm),
    )


def require_live(
    layers: LiveLayers, *, model_name: str, l3b_llm: bool = False, l4_llm: bool = False
) -> None:
    """Fail unless the classifier is loaded and each wanted LLM stage runs ``model_name``.

    MailGuardPipeline turns a stage off with only a warning when its model is unknown, and a
    missing L1 artifact only logs a warning, so without this check a mis-set model name or
    a skipped prep step would report a weaker C3 than the real guard.
    """
    expected = f"OpenAIProvider:{model_name}"
    problems: list[str] = []
    if not layers.l1_classifier:
        problems.append(f"L1 classifier not loaded ({PREP_HINT})")
    wanted = {"l1_judge": True, "l2_llm": True, "l3b_llm": l3b_llm, "l4_llm": l4_llm}
    for field_name, on in wanted.items():
        got = getattr(layers, field_name)
        if on and got != expected:
            problems.append(f"{field_name} is {got}, expected {expected}")
        if not on and got is not None:
            problems.append(f"{field_name} is {got}, expected off")
    if problems:
        raise GuardEnvError("guard is not at full strength: " + "; ".join(problems))
