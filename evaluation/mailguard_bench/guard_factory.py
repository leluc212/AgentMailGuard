"""AgentMailGuard settings and pipelines for the benchmark, independent of any .env.

Follows the guard's own harness (agentmailguard/evaluation/harness.py ``build_pipeline``):
settings that ignore any .env, every guard judge on one registered model, and a ModelRegistry
that reads rag-email's guard_models.yaml by absolute path. The L1 classifier path points at the
pinned artifact. Needs ``mailguard`` importable (the ``make mailguard-*`` targets).
Task 7.19; ADR-0010; guard v3 (ADR-0015, plan_v3 Task 12): the configs are the guard's own v3
presets and the stage names are its v3 judges, with no v2 layer vocabulary in between.
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
    doc_llm: bool = False,
    output_llm: bool = False,
    judge_fail_closed: bool = True,
) -> MailGuardSettings:
    """Guard settings with every judge on ``model_name``, independent of any .env.

    The Doc Scanner's two judges (chunk and set, ``doc_llm``) and the Output Guard's judge
    (``output_llm``) stay off unless asked for, as in the guard's own settings. With
    ``judge_fail_closed`` (the guard's default) a failed or invalid judge answer is the verdict
    error ``judge_invalid:<stage>`` and human review; off is the v2 visible fallback, which a run
    asks for to compare with v2 (``--judge-fallback``). Absolute paths are used as given.
    """
    settings = MailGuardSettings(_env_file=None)
    gm = settings.guard_models
    gm.inbound_judge = gm.inbound_extractor = model_name
    gm.doc_judge = gm.doc_set_judge = gm.output_judge = model_name
    gm.models_path = str(models_path.resolve())
    settings.inbound_guard.ml_model_path = str(l1_model_path.resolve())
    settings.doc_scanner.llm_enabled = doc_llm
    settings.doc_scanner.set_judge_llm_enabled = doc_llm
    settings.output_guard.llm_enabled = output_llm
    settings.mailguard.fail_closed_on_judge_error = judge_fail_closed
    return settings


def build_guard_pipeline(preset: str, settings: MailGuardSettings) -> MailGuardPipeline:
    """A pipeline for one guard v3 preset (``GuardConfig.preset``), audit log off."""
    return MailGuardPipeline(
        settings, GuardConfig.preset(preset), registry=ModelRegistry(settings), audit=False
    )


def active_switches(config: GuardConfig) -> tuple[str, ...]:
    """The switches a ``GuardConfig`` turns on: its active layers, then channel isolation."""
    return (
        *config.active_layers,
        *(("channel_isolation",) if config.channel_isolation else ()),
    )


@dataclass(frozen=True)
class LiveLayers:
    """Which guard parts are really live; recorded in the run manifest."""

    preset: str
    active_layers: tuple[str, ...]
    classifier: bool
    inbound_judge: str | None
    inbound_extractor: str | None
    doc_judge: str | None
    doc_set_judge: str | None
    output_judge: str | None

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
    set_judge = pipeline.doc_scanner.set_judge
    return LiveLayers(
        preset=str(pipeline.config.name),
        active_layers=active_switches(pipeline.config),
        classifier=bool(pipeline.inbound_guard.classifier.available),
        inbound_judge=provider_label(pipeline.inbound_guard.judge),
        inbound_extractor=provider_label(pipeline.inbound_guard.extractor_llm),
        doc_judge=provider_label(pipeline.doc_scanner.llm),
        doc_set_judge=provider_label(set_judge.llm if set_judge is not None else None),
        output_judge=provider_label(pipeline.output_guard.llm),
    )


def require_live(
    layers: LiveLayers, *, model_name: str, doc_llm: bool = False, output_llm: bool = False
) -> None:
    """Fail unless the classifier is loaded and each wanted judge runs ``model_name``.

    MailGuardPipeline turns a stage off with only a warning when its model is unknown, and a
    missing classifier only logs a warning, so without this check a mis-set model name or a
    skipped prep step would report a weaker C3 than the real guard.

    Only the layers the preset runs are checked (``layers.active_layers``): C3-L1 has no
    Inbound Guard judge, C3-L2 no Doc Scanner judge. The classifier is needed wherever the
    Inbound Guard or the Doc Scanner runs.
    """
    expected = f"OpenAIProvider:{model_name}"
    active = set(layers.active_layers)
    problems: list[str] = []
    if active & {"inbound_guard", "doc_scanner"} and not layers.classifier:
        problems.append(f"L1 classifier not loaded ({PREP_HINT})")
    wanted = {
        "inbound_judge": ("inbound_guard", True),
        "inbound_extractor": ("inbound_guard", True),
        "doc_judge": ("doc_scanner", doc_llm),
        "doc_set_judge": ("doc_scanner", doc_llm),
        "output_judge": ("output_guard", output_llm),
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
