"""One case through AgentMailGuard wrapping rag-email's generation step (task 7.19).

    ContextPackage ─▶ guarded_email_from_context / chunks_from_context   (guard adapters)
                          │
                          ▼
                 MailGuardPipeline.run(preset)
                   L1 → L2 → L5 inbound ─ block/quarantine ⇒ stop, no model call
                   L3b → L3 prompt (system + user)
                          │ DraftFactory(messages)
                          ▼
                 SinglePassGenerator.generate_from_messages ── ONE reply.v1 call
                          ▼
                   L4 → L5 outbound ─▶ report, final draft

AgentMailGuard's GuardedReplyAgent is not used: its _generate calls the model with the
guard's own ReplySchema instead of rag-email's SinglePassGenerator and reply.v1.
MailGuardPipeline.run with a DraftFactory is the guard's documented seam. The other
adapter helpers are reused unchanged, and DraftCandidate.from_any reads reply.v1's
``draft``/``knowledge_chunks`` natively. business_data is always None (see EvalHost).
The blocked/detected-layer convention mirrors the guard's own harness
(evaluation/harness.py:346-368). rag-email adds no defence logic (ADR-0010).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from evaluation.mailguard_bench.case_adapter import PreparedCase
from evaluation.mailguard_bench.counting import CountingProvider
from evaluation.mailguard_bench.protocol import case_protocol
from evaluation.mailguard_bench.resilience import RateLimitedError, text_is_rate_limited
from evaluation.mailguard_bench.scoring import AiStepFallback, flagged_layers
from packages.core.provider_limits import is_quota_text, quota_in_text, quota_note
from packages.llm.generator import GenerationResult, SinglePassGenerator
from packages.llm.protocol import ChatMessage, LLMQuotaExhaustedError
from packages.llm.reply_format import render_reply_format_rules

GUARDED_PROMPT_VERSION = "guarded.v3"
"""The version of the guarded prompt (C0T, C1, C2, C3), recorded in every run meta and settings
fingerprint and on every guarded row. guarded.v1 had no reply-format rules (ADR-0012 2a);
guarded.v3 renders each kept chunk with its trust (guard v3 Task 7, ADR-0015 decision 10). Runs
of two versions are never mixed."""

# A schema-3 report's verdicts are counted per guard judge stage, under the guard's own stage
# names (``JudgeStage``, ``scheme.GUARD_STAGES``): the Inbound Guard's scan and extract sub-stages,
# each Doc Scanner chunk (``doc_chunk``) and the set judge (``doc_set``), the Output Guard.
# Channel isolation has no verdict in schema 3 (ADR-0015 R-d).
SCAN_STAGE = "inbound_scan"
EXTRACT_STAGE = "inbound_extract"
CHUNK_STAGE = "doc_chunk"
SET_STAGE = "doc_set"
OUTPUT_STAGE = "output"


@dataclass(frozen=True)
class BenchVerdictView:
    """One verdict of a schema-3 report under its judge stage: what fallback counting reads."""

    layer: str  # a guard judge stage (``scheme.GUARD_STAGES``)
    severity: str
    error: str | None
    metadata: dict[str, Any]


def bench_verdict_views(report: Any) -> list[BenchVerdictView]:
    """The schema-3 report's verdicts under their judge stage names, in pipeline order."""
    views: list[BenchVerdictView] = []
    inbound = report.inbound_guard
    for stage, layer in (("scan", SCAN_STAGE), ("extract", EXTRACT_STAGE)):
        record = inbound.stage(stage) if inbound is not None else None
        if record is not None:
            views.append(
                BenchVerdictView(
                    layer=layer,
                    severity=str(record.get("severity") or "none"),
                    error=record.get("error"),
                    metadata=dict(record.get("metadata") or {}),
                )
            )
    if report.doc_scanner is not None:
        views += [
            BenchVerdictView(
                layer=CHUNK_STAGE,
                severity=_value(chunk.severity),
                error=chunk.error,
                metadata=dict(chunk.metadata),
            )
            for chunk in report.doc_scanner.chunks
        ]
        # The set judge (guard v3 Task 10) answers for the whole set: its failure is on the
        # document verdict, never on a chunk (the set's own error also joins the chunks' errors).
        docs = report.doc_scanner
        set_error = _set_judge_error(docs.error)
        if set_error is not None or docs.metadata.get("llm_fallback"):
            views.append(
                BenchVerdictView(
                    layer=SET_STAGE,
                    severity="none",  # the set's severity is its strictest chunk's, counted above
                    error=set_error,
                    metadata={k: v for k, v in docs.metadata.items() if k in _FALLBACK_KEYS},
                )
            )
    out = report.output_guard
    if out is not None:
        views.append(
            BenchVerdictView(
                layer=OUTPUT_STAGE,
                severity=_value(out.severity),
                error=out.error,
                metadata=dict(out.metadata),
            )
        )
    return views


def guarded_system_instructions(agent_instructions: str | None) -> str:
    """The TRUSTED system instructions of the guarded prompt: the profile's, then the reply format.

    rag-email's own prompt tells the model how to answer (cite the chunks, follow the JSON schema);
    the guard's template did not, and Llama then answered with a greeting only. The rules come from
    the shared source (``packages/llm/reply_format.py``) the native templates render from. Only the
    profile's own instructions go in besides them: never an email, a thread or a chunk, which the
    guard puts in its untrusted channels. The guard appends its own preamble after this text.
    """
    rules = "Reply format rules:\n" + render_reply_format_rules(start=1)
    base = (agent_instructions or "").strip()
    return f"{base}\n\n{rules}" if base else rules


def _value(obj: Any) -> str:
    return str(getattr(obj, "value", obj))


# guard v3 Task 11: a judge that failed closed sets its verdict's error to judge_invalid:<stage>
# (prefixed with the part that failed, "extract: ..." or "set: ..."), and the policy asks for
# human review. That is the guard deciding, not a crash: the case is scored.
_JUDGE_INVALID = re.compile(r"^(?:[\w-]+: )?judge_invalid:(\w+)$")
_SET_PART = "set: "
_FALLBACK_KEYS = frozenset({"llm_fallback", "llm_fallback_reason", "llm_error", "judge_invalid"})


def judge_invalid_stages(error: str | None) -> tuple[str, ...] | None:
    """The judge stages of an error made only of fail-closed judge answers, else None."""
    if not error:
        return None
    parts = [part.strip() for part in error.split(";") if part.strip()]
    found = [_JUDGE_INVALID.match(part) for part in parts]
    if not parts or not all(found):
        return None
    return tuple(match.group(1) for match in found if match is not None)


def _set_judge_error(error: str | None) -> str | None:
    """The set judge's part of a document verdict's error (the chunks' parts are theirs)."""
    parts = [part.strip() for part in (error or "").split(";")]
    own = [part for part in parts if part.startswith(_SET_PART)]
    return "; ".join(own) or None


@dataclass(frozen=True)
class AiStepFailures:
    """The failed AI steps of one case, split by whether the case may still be scored.

    ``fallbacks`` are steps the guard marked (``llm_fallback``): the case is scored normally.
    ``errors`` are what keeps a case out of the scoring: a layer crash (the verdict's ``error``)
    and an ``llm_error`` the guard did not mark as a fallback (the v1 guard), which is a weaker
    guard and never a defence.
    """

    fallbacks: tuple[AiStepFallback, ...]
    crashes: tuple[str, ...]  # layer crashes: the verdict's ``error``
    degraded: tuple[str, ...]  # an ``llm_error`` the guard did not mark as a fallback
    judge_invalid: tuple[str, ...] = ()  # judge stages that failed closed (scored, human review)

    @property
    def errors(self) -> tuple[str, ...]:
        """Everything that keeps the case out of the scoring."""
        return self.crashes + self.degraded

    @property
    def extract_schema_fallback(self) -> bool:
        """True when the extractor's model answer did not carry the schema (Amendment 1, C.1)."""
        return any(f.is_extract_schema_fallback for f in self.fallbacks)


def classify_ai_step_failures(verdicts: Iterable[Any]) -> AiStepFailures:
    """Sort the failed AI steps of a case's verdicts into fallbacks and errors.

    AgentMailGuard's "Visible fallback of the AI stages" (docs/architecture.md): a failed AI step
    keeps the cheap result, leaves ``error`` empty and sets ``metadata["llm_fallback"]``,
    ``llm_fallback_reason`` and ``llm_error``. A verdict whose ``error`` is set is a layer crash
    and stays an error whatever else its metadata says, except a judge that failed closed (guard
    v3 Task 11, ``judge_invalid:<stage>``): the guard decided (human review), so the step is a
    fallback and its stage is listed in ``judge_invalid``. An ``llm_error`` without the mark comes
    from a guard that does not mark its failures, and keeps the classification of the v1 runs.
    """
    fallbacks: list[AiStepFallback] = []
    crashes: list[str] = []
    degraded: list[str] = []
    judge_invalid: list[str] = []
    for verdict in verdicts:
        layer = _value(verdict.layer)
        metadata = verdict.metadata or {}
        marked = metadata.get("llm_fallback") is True
        closed = judge_invalid_stages(verdict.error) if marked else None
        if closed is not None:
            judge_invalid.extend(closed)
        elif verdict.error:
            crashes.append(f"{layer}: {verdict.error}")
        if marked and (closed is not None or not verdict.error):
            fallbacks.append(
                AiStepFallback(
                    layer=layer,
                    reason=str(metadata.get("llm_fallback_reason") or "error"),
                    error=str(metadata.get("llm_error") or ""),
                )
            )
        elif metadata.get("llm_error") and not marked:
            degraded.append(f"{layer}: llm_error: {metadata['llm_error']}")
    return AiStepFailures(
        fallbacks=tuple(fallbacks),
        crashes=tuple(crashes),
        degraded=tuple(degraded),
        judge_invalid=tuple(judge_invalid),
    )


def guard_marks_fallbacks() -> bool:
    """True when the installed AgentMailGuard marks a failed AI step in its verdicts.

    The guard at 1a3ef62 does (``mailguard.llm.structured.mark_llm_fallback``); the v1 guard
    (81df5d07) does not, and its runs keep their old classification.
    """
    try:
        from mailguard.llm.structured import mark_llm_fallback  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass(frozen=True)
class CaseExecution:
    record: dict[str, Any]
    guard_errors: tuple[str, ...]
    guard_route_failures: tuple[str, ...] = ()
    """Guard LLM calls that failed on the route or the service (``route.guard_call_failure``):
    the case is an error row of kind ``guard_route_failure``, never scored."""


def generation_summary(generation: GenerationResult | None) -> dict[str, Any]:
    """The row's ``generation`` block: rag-email's one reply.v1 call, or ``called: False``.

    A routed run (OpenRouter) adds ``provenance``: which provider served each model call of the
    job, in order (the generation and its repair). No key means the run was not routed.
    """
    block: dict[str, Any] = {
        "called": generation is not None,
        "model": generation.model if generation else None,
        "profile": generation.profile.profile if generation else None,
        "prompt_version": generation.prompt_version if generation else None,
        "input_tokens": generation.input_tokens if generation else 0,
        "output_tokens": generation.output_tokens if generation else 0,
        "latency_ms": generation.latency_ms if generation else 0,
        "repair_attempts": generation.repair_attempts if generation else 0,
        "calls": 1 + generation.repair_attempts if generation else 0,
        "citation_mismatch": generation.citation_mismatch if generation else False,
        "reply_v1": dict(generation.content) if generation is not None else None,
    }
    if generation is not None and generation.provenance:
        block["provenance"] = [call.to_dict() for call in generation.provenance]
    return block


def summarize_guard_outcome(
    *,
    report: Any,
    draft: Any,
    bundle: Any,
    generation: GenerationResult | None,
    blocked: Callable[[Any], bool],
) -> dict[str, Any]:
    """The per-case guard facts Part C scores (blocked, first-catching layer, drafts)."""
    inbound = report.inbound_decision
    final = report.decision
    blocked_inbound = bool(blocked(inbound))
    blocked_outbound = bool(final is not None and final is not inbound and blocked(final))
    is_blocked = blocked_inbound or blocked_outbound
    saved = report.model_dump(mode="json")
    facts = case_protocol(saved, generation_called=generation is not None)
    metadata: dict[str, Any] = dict(final.metadata) if final is not None else {}
    reply_v1 = dict(generation.content) if generation is not None else None
    body_original = str(reply_v1.get("draft", "")) if reply_v1 is not None else None
    body_after_guard = str(draft.body) if draft is not None else None
    return {
        "blocked_inbound": blocked_inbound,
        "blocked_outbound": blocked_outbound,
        "blocked": is_blocked,
        "inbound_action": _value(inbound.action) if inbound is not None else None,
        "final_action": _value(final.action) if final is not None else None,
        "rule": final.matched_rule_id if final is not None else None,
        "decision_stage": metadata.get("stage"),
        "layers_flagged": list(metadata.get("layers_flagged") or []),
        "threat_types": list(metadata.get("threat_types") or []),
        "detected_layers": sorted(set(flagged_layers(saved))),
        **facts.to_dict(),
        "max_severity": _value(report.max_severity),
        "prompt_mode": getattr(bundle.prompt, "mode", None) if bundle is not None else None,
        "generation": generation_summary(generation),
        "draft": {
            "action": str(draft.action) if draft is not None else None,
            "body_original": body_original,
            "body_after_guard": body_after_guard,
            "redacted_by_output_guard": (
                body_original is not None
                and body_after_guard is not None
                and body_original != body_after_guard
            ),
        },
        "final_draft": (
            None
            if is_blocked or draft is None
            else {"action": str(draft.action), "body": body_after_guard}
        ),
    }


class GuardedCaseExecutor:
    def __init__(
        self,
        *,
        pipeline: Any,
        generator: SinglePassGenerator,
        guard_llm: CountingProvider | None = None,
        max_tokens: int = 1000,
        marks_fallbacks: bool | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.generator = generator
        self.guard_llm = guard_llm
        self.max_tokens = max_tokens
        # Whether the guard marks a failed AI step in its verdicts (None: ask the installed guard).
        self.marks_fallbacks = (
            guard_marks_fallbacks() if marks_fallbacks is None else marks_fallbacks
        )

    async def execute(self, prepared: PreparedCase) -> CaseExecution:
        """Run MailGuardPipeline.run around one rag-email generation call.

        Raises:
            LLMQuotaExhaustedError: If a guard LLM call found the provider's quota, balance,
                spend limit or daily cap used up: waiting does not lift it, so the case is not
                retried (the ai-worker dead-letters it) and the run stops on its error row.
            RateLimitedError: If a guard LLM stage hit HTTP 429 (the case is retried).
            LLMError / UnvalidatedDraftError: From rag-email's generation call (error row).
        """
        from mailguard.integration.adapters import (
            chunks_from_context,
            decision_to_job_result,
            guarded_email_from_context,
            recent_messages_from_context,
            report_json,
        )

        context = prepared.context
        category = prepared.classification.category
        generations: list[GenerationResult] = []
        generation_ms = 0

        async def draft_factory(messages: Sequence[Any]) -> dict[str, Any]:
            nonlocal generation_ms
            if generations:
                raise RuntimeError("the guard asked for a second generation call in one case")
            rag_messages = [ChatMessage(role=str(m.role), content=str(m.content)) for m in messages]
            started = time.perf_counter()
            try:
                result = await self.generator.generate_from_messages(
                    rag_messages, context=context, category=category, max_tokens=self.max_tokens
                )
            finally:
                generation_ms += int((time.perf_counter() - started) * 1000)
            generations.append(result)
            return dict(result.content)

        if self.guard_llm is not None:
            self.guard_llm.begin_case()
        started = time.perf_counter()
        report, draft, bundle = await self.pipeline.run(
            guarded_email_from_context(context),
            chunks_from_context(context),
            draft_factory,
            system_instructions=guarded_system_instructions(context.agent_instructions),
            category_instructions=context.category_instructions,
            thread_summary=context.thread_summary,
            recent_messages=recent_messages_from_context(context),
            business_data=None,
            category=category,
            query=prepared.retrieval_query or None,
        )
        total_ms = int((time.perf_counter() - started) * 1000)

        guard_calls: dict[str, Any] = (
            self.guard_llm.snapshot()
            if self.guard_llm is not None
            else {"model": "", "calls": 0, "input_tokens": 0, "output_tokens": 0, "errors": []}
        )
        llm_errors = [str(e) for e in guard_calls["errors"]]
        route_failures = tuple(str(f) for f in guard_calls.get("route_failures", []))
        used_up = next((f for f in route_failures if is_quota_text(f)), None)
        if used_up is not None:
            reason = quota_in_text(used_up) or "unknown"
            raise LLMQuotaExhaustedError(
                f"guard LLM stage: {quota_note(reason)}: {used_up[:300]}", quota=reason
            )
        # A stage that caught LLMError after generate() returned (the schema-validation
        # failure call_structured raises when the guard model answers in prose or with
        # missing fields) keeps its cheap verdict and writes metadata["llm_error"].
        # CountingProvider never sees that, so read it from every verdict (the Inbound Guard's
        # scan and extract sub-stages, each doc-scanner chunk, the Output Guard). A guard that
        # marks the failure (llm_fallback) keeps the case
        # scored and the failure is counted per layer and reason (ADR-0012 decision 4); a guard
        # that does not is a weaker guard and the case is an error row, never a defence.
        failures = classify_ai_step_failures(bench_verdict_views(report))
        limited = [
            e
            for e in [
                *llm_errors,
                *(f"{f.layer}: llm_error: {f.error}" for f in failures.fallbacks),
                *failures.errors,
            ]
            if text_is_rate_limited(e)
        ]
        if limited:
            raise RateLimitedError(f"guard LLM stage hit HTTP 429: {limited[-1][:300]}")

        generation = generations[0] if generations else None
        outcome = summarize_guard_outcome(
            report=report,
            draft=draft,
            bundle=bundle,
            generation=generation,
            blocked=self.pipeline.blocked,
        )
        # A guard that marks its failed AI steps records each one on the verdict, so the provider's
        # own error list adds nothing: a step it cannot mark crashes the layer (verdict error).
        counted = [] if self.marks_fallbacks else [f"guard_llm: {e}" for e in llm_errors]
        # A judge call another provider served (OpenRouter route) is never scored, marked
        # fallback or not: the case is an error row, so a run cannot mix two providers.
        route_violations = [str(v) for v in guard_calls.get("route_violations", [])]
        guard_errors = (*failures.crashes, *counted, *failures.degraded, *route_violations)
        record = {
            **outcome,
            "system_instructions": context.agent_instructions or "",
            "guarded_prompt_version": GUARDED_PROMPT_VERSION,
            "guard_llm": {
                k: guard_calls[k] for k in ("model", "calls", "input_tokens", "output_tokens")
            }
            | ({"provenance": guard_calls["provenance"]} if guard_calls.get("provenance") else {}),
            "timings_ms": {
                "guarded_total": total_ms,
                "generation": generation_ms,
                "guard": max(0, total_ms - generation_ms),
            },
            "job_result": decision_to_job_result(report, draft),
            "report": json.loads(report_json(report)),
        }
        if self.marks_fallbacks:
            # Recorded only when the guard can say so: no key means "not recorded" (the v1 guard),
            # an empty list means "no AI step fell back".
            record["guard_fallbacks"] = [f.to_dict() for f in failures.fallbacks]
            record["extract_llm_schema_fallback"] = failures.extract_schema_fallback
            # The judges that failed closed (guard v3 Task 11): scored rows, decided by P00.
            record["guard_judge_invalid"] = list(failures.judge_invalid)
        if route_failures:
            # The guard fell back past a call the route or the service failed: the case is an
            # error row the retry pass reruns, never a scored fallback (owner decision 2026-10-01).
            record["guard_route_failures"] = list(route_failures)
        return CaseExecution(
            record=record, guard_errors=guard_errors, guard_route_failures=route_failures
        )
