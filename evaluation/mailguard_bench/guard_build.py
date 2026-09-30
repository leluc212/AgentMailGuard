"""Build AgentMailGuard's pipeline for one guarded benchmark config, with counted guard calls.

(evaluation/harness.py:438 on feature/mailguard-defense-stack; task 7.19; ADR-0010.)

Settings come from ``guard_factory.guard_settings`` (Task 1): MailGuardSettings(_env_file=None),
all four guard model names on one model registered in ``guard_models.yaml``, and the L1
classifier trained by ``make mailguard-prep``. The providers are passed explicitly (one
CountingProvider) because MailGuardPipeline would otherwise turn an unknown model into a
disabled stage with only a log warning; ``ModelRegistry.get`` raises instead. The L5 audit
log goes to the run folder, so the AgentMailGuard worktree stays clean. mailguard is
imported lazily, so this module imports in CI where AgentMailGuard is not installed.

Configs (owner decision 2026-09-29, plan BINDING section): ``C0`` is rag-email's native
path and runs no AgentMailGuard code, so it has no guard to build here; ``C0T`` is the
guard's template with ``GuardConfig.preset("C0")`` (no layer active); ``C1``/``C2``/``C3``
are the presets of the same name. The layer ablation (task 7.22, pre-registration
2026-09-30) adds ``C3-L1`` ... ``C3-L5``: the guard's own "C3 minus one layer" presets.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaluation.mailguard_bench.counting import CountingProvider
from evaluation.mailguard_bench.guard_env import GUARD_MODELS_YAML, checkout_commit, sha256_file

NATIVE_CONFIG = "C0"
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
# benchmark config -> AgentMailGuard GuardConfig preset
GUARDED_CONFIGS: dict[str, str] = {
    "C0T": "C0",
    "C1": "C1",
    "C2": "C2",
    "C3": "C3",
    **{config: config for config in ABLATION_CONFIGS},
}
BENCH_PRESETS = ("C0T", "C1", "C2", "C3")  # the v1 guarded configs; the ablation adds more
FULL_GUARD_CONFIG = "C3"
"""The config that runs the whole guard: the live v2 benchmark also gives it the LLM stages."""


def git_head(path: Path) -> str | None:
    """HEAD of the checkout at ``path``, or None outside git.

    For a guard subtree (task 7.24) the pinned commit whose tree it holds instead, None if none:
    the enclosing repository's HEAD is not the guard's commit.
    """
    return checkout_commit(path)


@dataclass
class GuardBuild:
    config: str
    preset: str
    model_name: str
    pipeline: Any
    guard_llm: CountingProvider

    def live_stages(self) -> dict[str, bool]:
        pipeline = self.pipeline
        settings = pipeline.settings
        return {
            "l1.classifier": bool(pipeline.l1.classifier.available),
            "l1.judge": pipeline.l1.judge is not None and bool(settings.l1.llm_enabled),
            "l2.llm": pipeline.l2.llm is not None and bool(settings.l2.llm_enabled),
            "l3b.llm": pipeline.l3b.llm is not None,
            "l4.llm": pipeline.l4.llm is not None,
        }

    def missing_live_stages(self) -> list[str]:
        """Stages the preset needs that are not live (a C3 that would silently be weaker).

        Only stages of layers the preset runs count: C3-L1 has no L1 judge, C3-L2 no L2 LLM
        step. The trained classifier is needed wherever a layer that reads it runs: L1, and
        also L2 and L3b, which the pipeline hands L1's classifier (a C3-L1 without it would
        be a weaker guard than the preset).
        """
        config = self.pipeline.config
        settings = self.pipeline.settings
        live = self.live_stages()
        required: list[str] = []
        if config.l1 or config.l2 or config.l3b:
            required.append("l1.classifier")
        if config.l1 and settings.l1.llm_enabled:
            required.append("l1.judge")
        if config.l2 and settings.l2.llm_enabled:
            required.append("l2.llm")
        if config.l3b and settings.l3b.llm_enabled:
            required.append("l3b.llm")
        if config.l4 and settings.l4.llm_enabled:
            required.append("l4.llm")
        return [stage for stage in required if not live[stage]]

    def describe(self) -> dict[str, Any]:
        """Guard facts for raw/<CONFIG>.meta.json and manifest.json (spec §4b artifacts)."""
        from mailguard.config.settings import PROJECT_ROOT

        from evaluation.mailguard_bench.guard_factory import live_layers

        settings = self.pipeline.settings
        l1_path = Path(settings.resolve(settings.l1.ml_model_path))
        return {
            "config": self.config,
            "preset": self.preset,
            "active_layers": list(self.pipeline.config.active_layers),
            "guard_model": self.model_name,
            "live_stages": self.live_stages(),
            "missing_live_stages": self.missing_live_stages(),
            "live_layers": live_layers(self.pipeline).as_dict(),
            "l1_model_path": str(l1_path),
            "l1_model_sha256": sha256_file(l1_path) if l1_path.exists() else None,
            "mailguard_root": str(PROJECT_ROOT),
            "mailguard_commit": git_head(Path(PROJECT_ROOT)),
            "audit_log_path": settings.l5.audit_log_path,
        }


def live_guard_llm_stages(config: str) -> tuple[bool, bool]:
    """``(l3b_llm, l4_llm)``: the optional guard LLM stages a live v2 run of ``config`` uses.

    C3, the full guard, runs both; C0T, C1 and C2 keep them off, as every v1 guard does (task
    7.19). The guard-worker builds its guard with this rule, and so must anything that
    describes that guard (the runner's fingerprint), or the report would name the stages of a
    guard the run never used.
    """
    full = config.upper() == FULL_GUARD_CONFIG  # build_guard reads the preset in any case
    return full, full


def build_guard(
    preset: str,
    *,
    model_name: str,
    audit_log_path: Path,
    l1_model_path: Path,
    models_path: Path = GUARD_MODELS_YAML,
    l3b_llm: bool = False,
    l4_llm: bool = False,
) -> GuardBuild:
    """MailGuardPipeline for C0T|C1|C2|C3|C3-L1..C3-L5, its LLM stages on ``model_name``.

    The L1 judge and the L2 extractor always run on the model. L3b's document check and L4's
    output check are off unless asked for: v1 runs (task 7.19) keep them off, and the live v2
    benchmark (task 7.20, ADR-0011) turns both on for C3 (``live_guard_llm_stages``).

    Args:
        preset: The benchmark config; ``C0T`` builds ``GuardConfig.preset("C0")``, the
            ablation configs the guard's "C3 minus one layer" preset of the same name.
        l3b_llm: Also run L3b's LLM poisoned-document check on ``model_name``.
        l4_llm: Also run L4's LLM output check on ``model_name``.

    Raises:
        ValueError: If ``preset`` is ``C0`` (rag-email's native path has no guard) or not
            one of the guarded benchmark configs.
        KeyError: If ``model_name`` is not registered (never silently disabled).
    """
    key = preset.upper()
    if key == NATIVE_CONFIG:
        raise ValueError(
            "C0 is rag-email's native path (generate_draft, no AgentMailGuard code) and has "
            "no guard to build; C0T is the guard template with no layer active"
        )
    if key not in GUARDED_CONFIGS:
        raise ValueError(
            f"benchmark preset must be one of {tuple(GUARDED_CONFIGS)}, got {preset!r}"
        )
    guard_preset = GUARDED_CONFIGS[key]
    from mailguard.llm.registry import ModelRegistry
    from mailguard.pipeline import GuardConfig, MailGuardPipeline

    from evaluation.mailguard_bench.guard_factory import guard_settings

    settings = guard_settings(
        model_name,
        l1_model_path=l1_model_path,
        models_path=models_path,
        l3b_llm=l3b_llm,
        l4_llm=l4_llm,
    )
    settings.l5.audit_log_path = str(audit_log_path.resolve())
    registry = ModelRegistry(settings)
    guard_llm = CountingProvider(registry.get(model_name))
    pipeline = MailGuardPipeline(
        settings,
        GuardConfig.preset(guard_preset),
        registry=registry,
        judge=guard_llm,
        extractor_llm=guard_llm,
        doc_llm=guard_llm if settings.l3b.llm_enabled else None,
        output_llm=guard_llm if settings.l4.llm_enabled else None,
        audit=True,
    )
    return GuardBuild(
        config=key,
        preset=guard_preset,
        model_name=model_name,
        pipeline=pipeline,
        guard_llm=guard_llm,
    )
