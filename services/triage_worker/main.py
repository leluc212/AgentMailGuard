"""Triage Worker Service entrypoint (RA.9; R6.1, R6.2, R6.5, R7.1, R3.4).

Consumes triage jobs from 'email.triage', runs the cascading classifier
(rules -> ML -> LLM), evaluates the early-exit gate, and routes actionable mail to
'email.route' as 'email.<category>.<lane>'. Missing rules, templates, or model files stop
the worker at startup instead of degrading silently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from aio_pika.abc import AbstractRobustConnection

from packages.broker.publisher import MessagePublisher
from packages.broker.worker_runtime import StartFn, WorkerResources, WorkerRuntime
from packages.core.settings import AppSettings, TriageWorkerSettings
from packages.db.draft import DraftStore, PostgresDraftStore
from packages.db.job import JobStore, PostgresJobStore
from packages.db.message import MessageStore, PostgresMessageStore
from packages.domain.templates import TemplateRegistry
from packages.llm.factory import create_llm_provider
from packages.observability.shutdown import GracefulShutdownCoordinator
from services.triage_worker.cascade import CascadingTriageEngine
from services.triage_worker.classifier import MLClassifier
from services.triage_worker.consumer import TriageConsumer
from services.triage_worker.gate import EarlyExitGate
from services.triage_worker.llm_classifier import LLMTriageClassifier
from services.triage_worker.rules import HotReloadableRuleEngine
from services.triage_worker.template_loader import load_templates_from_file
from services.triage_worker.thresholds import ThresholdManager

logger = logging.getLogger("triage_worker.service")

SERVICE_NAME = "triage_worker"
DEFAULT_PORT = 8003
_FILE_BODY_SUFFIXES = (".txt", ".j2", ".md")


class TriageWorkerConfigError(RuntimeError):
    """A required triage asset is missing or unusable; the worker must not start."""


def _require_file(raw_path: str, what: str) -> Path:
    path = Path(raw_path)
    if not path.is_file():
        raise TriageWorkerConfigError(
            f"{what} not found at '{path}' (resolved against cwd '{Path.cwd()}')"
        )
    return path


def _load_rule_engine(rules_path: Path) -> HotReloadableRuleEngine:
    # HotReloadableRuleEngine swallows parse errors and falls back to an empty engine;
    # an empty rule set at startup is a misconfiguration, not a default.
    engine = HotReloadableRuleEngine(rules_path=rules_path)
    if engine.rules_count == 0:
        raise TriageWorkerConfigError(
            f"Triage rules file '{rules_path}' produced zero rules (invalid YAML or empty list)"
        )
    return engine


def _load_template_registry(templates_path: Path) -> TemplateRegistry:
    try:
        registry = load_templates_from_file(templates_path)
    except Exception as exc:
        raise TriageWorkerConfigError(
            f"Invalid response templates file '{templates_path}': {exc}"
        ) from exc
    # The gate renders without base_dir, so file bodies resolve against cwd only; an
    # unresolved body would be rendered as the literal path string (templates.py).
    for template in registry.templates:
        body = template.body
        if body.endswith(_FILE_BODY_SUFFIXES) and registry.resolve_body(template) == body:
            raise TriageWorkerConfigError(
                f"Template '{template.id}' body file '{body}' not found "
                f"(resolved against cwd '{Path.cwd()}')"
            )
    return registry


def _load_ml_classifier(model_path: Path) -> MLClassifier:
    try:
        return MLClassifier.load_from_artifact(model_path)
    except (FileNotFoundError, ValueError) as exc:
        raise TriageWorkerConfigError(str(exc)) from exc


def build_triage_consumer(
    settings: AppSettings,
    *,
    publisher: MessagePublisher,
    job_store: JobStore,
    message_store: MessageStore,
    draft_store: DraftStore,
    connection: AbstractRobustConnection | None = None,
    shutdown_coordinator: GracefulShutdownCoordinator | None = None,
) -> TriageConsumer:
    """Compose the production TriageConsumer from settings and injected stores."""
    triage_cfg = settings.triage
    rule_engine = _load_rule_engine(_require_file(triage_cfg.rules_path, "Triage rules file"))
    template_registry = _load_template_registry(
        _require_file(triage_cfg.templates_path, "Response templates file")
    )
    ml_classifier = _load_ml_classifier(
        _require_file(triage_cfg.ml_model_path, "ML classifier artifact")
    )

    if settings.llm.provider.strip().lower() == "fake":
        logger.warning(
            "LLM provider is 'fake': Stage 3 triage returns FakeLLMProvider's canned "
            "classification. Set LLM__PROVIDER for real classification."
        )
    llm_classifier = LLMTriageClassifier(provider=create_llm_provider(settings.llm))

    gate = EarlyExitGate(
        job_store=job_store,
        template_registry=template_registry,
        draft_store=draft_store,
    )
    cascade = CascadingTriageEngine(
        rule_engine=rule_engine,
        ml_classifier=ml_classifier,
        llm_classifier=llm_classifier,
        threshold_manager=ThresholdManager(settings=triage_cfg),
        template_registry=template_registry,
        draft_store=draft_store,
        gate=gate,
    )
    return TriageConsumer(
        cascade=cascade,
        gate=gate,
        publisher=publisher,
        broker_settings=settings.broker,
        retry_settings=settings.retry,
        prefetch_count=settings.concurrency.triage_worker_concurrency,
        connection=connection,
        job_store=job_store,
        message_store=message_store,
        shutdown_coordinator=shutdown_coordinator,
    )


async def build_components(res: WorkerResources) -> list[StartFn]:
    consumer = build_triage_consumer(
        res.settings,
        publisher=res.publisher,
        job_store=PostgresJobStore(res.db_pool),
        message_store=PostgresMessageStore(res.db_pool),
        draft_store=PostgresDraftStore(res.db_pool),
        connection=res.connection,
        shutdown_coordinator=res.shutdown,
    )
    aclose = getattr(consumer.cascade.llm_classifier.provider, "aclose", None)
    if aclose is not None:
        # Registered after the consumer's close(): the HTTP client closes after draining.
        res.shutdown.register_cleanup_callback(aclose)
    return [consumer.start]


def main() -> None:
    """Main process entrypoint."""
    runtime = WorkerRuntime(
        service_name=SERVICE_NAME,
        settings=TriageWorkerSettings(),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
        build=build_components,
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runtime.run())


if __name__ == "__main__":
    main()
