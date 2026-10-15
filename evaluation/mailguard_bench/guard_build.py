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
path and runs no AgentMailGuard code, so it has no guard to build here. Scheme v3 (guard v3,
ADR-0015, plan_v3 Task 12), the only scheme a run takes at HEAD: ``C0T`` is the guard's template
with ``GuardConfig.preset("C0")`` (nothing active); ``C1``, ``C2``, ``C3``, ``C3-L1`` ..
``C3-L4`` and ``C3-ISO`` are the guard's own presets of the same name (``scheme.V3_LAYERS``).
Every judge of a layer the config runs is live (``scheme.V3_AI_STAGES``). Schemes v1 and v2 ran
on other guard commits and are refused (``scheme.require_run_scheme``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from evaluation.mailguard_bench.counting import CountingProvider
from evaluation.mailguard_bench.guard_env import (
    GUARD_MODELS_YAML,
    GuardEnvError,
    checkout_commit,
    sha256_file,
)
from evaluation.mailguard_bench.route import guard_pin_problem

# re-exported for the readers of the committed v1 folders (report.py, runner.py's v1 names)
from evaluation.mailguard_bench.scheme import ABLATION_CONFIGS as ABLATION_CONFIGS
from evaluation.mailguard_bench.scheme import (
    JUDGE_FAIL_CLOSED_KEY,
    SCHEME_V3,
    V3_LAYERS,
    require_config,
    require_run_scheme,
    v3_guard_preset,
    v3_llm_stages,
    v3_required_stages,
)
from evaluation.mailguard_bench.scheme import LAYER_ABLATIONS as LAYER_ABLATIONS

NATIVE_CONFIG = "C0"
# scheme v1 benchmark config -> its guard preset (kept for the v1 readers; never built at HEAD)
GUARDED_CONFIGS: dict[str, str] = {
    "C0T": "C0",
    "C1": "C1",
    "C2": "C2",
    "C3": "C3",
    **{config: config for config in ABLATION_CONFIGS},
}
BENCH_PRESETS = ("C0T", "C1", "C2", "C3")  # the v1 guarded configs; the ablation adds more


def git_head(path: Path, expected_commit: str | None = None) -> str | None:
    """HEAD of the checkout at ``path``, or None outside git.

    For a guard subtree (task 7.24) the pinned commit whose tree it holds instead (see
    ``guard_env.checkout_commit``: ``expected_commit``, else ``MAILGUARD_COMMIT``), None if none:
    the enclosing repository's HEAD is not the guard's commit.
    """
    return checkout_commit(path, expected_commit)


@dataclass
class GuardBuild:
    config: str
    preset: str
    model_name: str
    pipeline: Any
    guard_llm: CountingProvider
    scheme: str = SCHEME_V3

    def live_stages(self) -> dict[str, bool]:
        """Every v3 stage (``scheme.V3_STAGES``) and whether the pipeline really has it live."""
        pipeline = self.pipeline
        settings = pipeline.settings
        set_judge = pipeline.doc_scanner.set_judge
        return {
            "classifier": bool(pipeline.inbound_guard.classifier.available),
            "inbound_judge": pipeline.inbound_guard.judge is not None
            and bool(settings.inbound_guard.scan_llm_enabled),
            "inbound_extractor": pipeline.inbound_guard.extractor_llm is not None
            and bool(settings.inbound_guard.extract_llm_enabled),
            "doc_judge": pipeline.doc_scanner.llm is not None
            and bool(settings.doc_scanner.llm_enabled),
            "doc_set_judge": set_judge is not None
            and set_judge.llm is not None
            and bool(settings.doc_scanner.set_judge_llm_enabled),
            "output_judge": pipeline.output_guard.llm is not None
            and bool(settings.output_guard.llm_enabled),
        }

    def missing_live_stages(self) -> list[str]:
        """Stages the config needs that are not live (a guard that would silently be weaker).

        A v3 config needs exactly the stages ``scheme.v3_required_stages`` lists: the trained
        classifier wherever the Inbound Guard or the Doc Scanner runs, and every judge of each
        layer it runs. A stage that list names counts as missing when it is off, even where the
        guard's own settings would have left it out.
        """
        live = self.live_stages()
        return [stage for stage in v3_required_stages(self.config) if not live[stage]]

    @property
    def judge_fail_closed(self) -> bool:
        return bool(self.pipeline.settings.mailguard.fail_closed_on_judge_error)

    def describe(self) -> dict[str, Any]:
        """Guard facts for raw/<CONFIG>.meta.json and manifest.json (spec §4b artifacts)."""
        from mailguard.config.settings import PROJECT_ROOT

        from evaluation.mailguard_bench.guard_factory import active_switches, live_layers

        settings = self.pipeline.settings
        l1_path = Path(settings.resolve(settings.inbound_guard.ml_model_path))
        routing = getattr(self.guard_llm, "provider_routing", None)
        return {
            "config": self.config,
            "scheme": self.scheme,
            "preset": self.preset,
            "active_layers": list(active_switches(self.pipeline.config)),
            "channel_isolation": bool(self.pipeline.config.channel_isolation),
            "guard_model": self.model_name,
            JUDGE_FAIL_CLOSED_KEY: self.judge_fail_closed,
            "live_stages": self.live_stages(),
            "missing_live_stages": self.missing_live_stages(),
            "live_layers": live_layers(self.pipeline).as_dict(),
            "l1_model_path": str(l1_path),
            "l1_model_sha256": sha256_file(l1_path) if l1_path.exists() else None,
            "mailguard_root": str(PROJECT_ROOT),
            "mailguard_commit": git_head(Path(PROJECT_ROOT)),
            "audit_log_path": settings.policy.audit_log_path,
            # An OpenRouter run: the pin the guard's judges send (no key means not routed, as v1)
            **({"provider_routing": dict(routing)} if isinstance(routing, Mapping) else {}),
        }


def live_guard_llm_stages(config: str, scheme: str = SCHEME_V3) -> tuple[bool, bool]:
    """``(doc_llm, output_llm)``: the opt-in guard judges a run of ``config`` uses.

    A v3 config runs every judge of each layer it runs (``scheme.v3_llm_stages``): the Doc
    Scanner's chunk and set judges with the Doc Scanner, the Output Guard's judge with the Output
    Guard. The guard-worker builds its guard with this rule, and so must anything that describes
    that guard (the runner's fingerprint), or the report would name the stages of a guard the run
    never used.

    Raises:
        ValueError: For a retired scheme (v1, v2) or a name that is not a v3 config.
    """
    require_run_scheme(scheme)
    return v3_llm_stages(config.upper())


def build_guard(
    preset: str,
    *,
    model_name: str,
    audit_log_path: Path,
    l1_model_path: Path,
    models_path: Path = GUARD_MODELS_YAML,
    doc_llm: bool = False,
    output_llm: bool = False,
    judge_fail_closed: bool = True,
    scheme: str = SCHEME_V3,
    expected_route: Mapping[str, Any] | None = None,
) -> GuardBuild:
    """MailGuardPipeline for one guarded v3 config, its judges on ``model_name``.

    The Inbound Guard's judge and extractor always run on the model. The Doc Scanner's judges and
    the Output Guard's judge are off unless asked for (``live_guard_llm_stages`` gives a config's).

    Args:
        preset: The benchmark config: ``C0T`` builds ``GuardConfig.preset("C0")``, every other
            guarded v3 config the guard's preset of the same name (``scheme.V3_LAYERS``).
        doc_llm: Also run the Doc Scanner's chunk judge and set judge on ``model_name``.
        output_llm: Also run the Output Guard's judge on ``model_name``.
        judge_fail_closed: A failed or invalid judge answer fails closed (the guard's default);
            False keeps the v2 visible fallback, to compare with v2.
        scheme: The config scheme; only v3 is built at HEAD.
        expected_route: The OpenRouter ``provider`` object the run pins (None: not routed). The
            guard's provider for ``model_name`` must send exactly it and ask for the router's
            metadata, or the build fails before any call (``route.guard_pin_problem``).

    Raises:
        GuardEnvError: If ``expected_route`` is set and the guard's provider would not honour it.
        ValueError: If ``preset`` is ``C0`` (rag-email's native path has no guard), not a v3
            config, or ``scheme`` is a retired scheme.
        KeyError: If ``model_name`` is not registered (never silently disabled).
    """
    require_run_scheme(scheme)
    key = preset.upper()
    if key == NATIVE_CONFIG:
        raise ValueError(
            "C0 is rag-email's native path (generate_draft, no AgentMailGuard code) and has "
            "no guard to build; C0T is the guard template with no layer active"
        )
    require_config(scheme, key)
    guard_preset = v3_guard_preset(key)
    from mailguard.llm.registry import ModelRegistry
    from mailguard.pipeline import GuardConfig, MailGuardPipeline

    from evaluation.mailguard_bench.guard_factory import active_switches, guard_settings

    settings = guard_settings(
        model_name,
        l1_model_path=l1_model_path,
        models_path=models_path,
        doc_llm=doc_llm,
        output_llm=output_llm,
        judge_fail_closed=judge_fail_closed,
    )
    settings.policy.audit_log_path = str(audit_log_path.resolve())
    registry = ModelRegistry(settings)
    provider = registry.get(model_name)
    problem = guard_pin_problem(provider, expected_route)
    if problem is not None:
        raise GuardEnvError(problem)
    guard_llm = CountingProvider(provider)
    config = replace(GuardConfig.preset(guard_preset), name=key)
    if active_switches(config) != V3_LAYERS[key]:  # scheme.py is the guard's own preset table
        raise ValueError(
            f"{key}: the guard's preset {guard_preset} turns on {active_switches(config)}, "
            f"scheme v3 expects {V3_LAYERS[key]}"
        )
    pipeline = MailGuardPipeline(
        settings,
        config,
        registry=registry,
        inbound_judge=guard_llm,
        inbound_extractor=guard_llm,
        doc_judge=guard_llm if settings.doc_scanner.llm_enabled else None,
        doc_set_judge=guard_llm if settings.doc_scanner.set_judge_llm_enabled else None,
        output_judge=guard_llm if settings.output_guard.llm_enabled else None,
        audit=True,
    )
    return GuardBuild(
        config=key,
        preset=guard_preset,
        model_name=model_name,
        pipeline=pipeline,
        guard_llm=guard_llm,
        scheme=scheme,
    )
