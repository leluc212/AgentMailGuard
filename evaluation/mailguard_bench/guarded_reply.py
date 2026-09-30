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
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from evaluation.mailguard_bench.case_adapter import PreparedCase
from evaluation.mailguard_bench.counting import CountingProvider
from evaluation.mailguard_bench.resilience import RateLimitedError, text_is_rate_limited
from packages.llm.generator import GenerationResult, SinglePassGenerator
from packages.llm.protocol import ChatMessage
from packages.llm.reply_format import render_reply_format_rules

L3B_LAYER = "l3b_document_scanner"
GUARDED_PROMPT_VERSION = "guarded.v2"
"""The version of the guarded prompt (C0T, C1, C2, C3), recorded in every run meta and settings
fingerprint and on every guarded row. guarded.v1 had no reply-format rules (ADR-0012 2a); a v1
guarded run and a v2 one are never mixed."""


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


@dataclass(frozen=True)
class CaseExecution:
    record: dict[str, Any]
    guard_errors: tuple[str, ...]


def generation_summary(generation: GenerationResult | None) -> dict[str, Any]:
    """The row's ``generation`` block: rag-email's one reply.v1 call, or ``called: False``."""
    return {
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
    detected = {
        _value(v.layer)
        for v in report.verdicts()
        if v.severity.rank >= 2 and _value(v.layer) != L3B_LAYER
    }
    quarantined = [str(chunk.chunk_id) for chunk in report.l3b if chunk.quarantined]
    if quarantined:
        detected.add(L3B_LAYER)
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
        "detected_layers": sorted(detected),
        "l3b_quarantined": quarantined,
        "max_severity": _value(report.max_severity),
        "prompt_mode": getattr(bundle.prompt, "mode", None) if bundle is not None else None,
        "generation": generation_summary(generation),
        "draft": {
            "action": str(draft.action) if draft is not None else None,
            "body_original": body_original,
            "body_after_guard": body_after_guard,
            "redacted_by_l4": (
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
    ) -> None:
        self.pipeline = pipeline
        self.generator = generator
        self.guard_llm = guard_llm
        self.max_tokens = max_tokens

    async def execute(self, prepared: PreparedCase) -> CaseExecution:
        """Run MailGuardPipeline.run around one rag-email generation call.

        Raises:
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
        # A stage that caught LLMError after generate() returned (the schema-validation
        # failure call_structured raises when the guard model answers in prose or with
        # missing fields) keeps its cheap verdict and only writes metadata["llm_error"].
        # CountingProvider never sees that, so read it from every verdict (L1, L2, L3,
        # each L3b chunk, L4): a weaker guard must be an error row, never a defence.
        degraded = [
            f"{_value(v.layer)}: llm_error: {v.metadata['llm_error']}"
            for v in report.verdicts()
            if (v.metadata or {}).get("llm_error")
        ]
        limited = [e for e in llm_errors + degraded if text_is_rate_limited(e)]
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
        guard_errors = tuple(
            [f"{_value(v.layer)}: {v.error}" for v in report.verdicts() if v.error]
            + [f"guard_llm: {e}" for e in llm_errors]
            + degraded
        )
        record = {
            **outcome,
            "system_instructions": context.agent_instructions or "",
            "guarded_prompt_version": GUARDED_PROMPT_VERSION,
            "guard_llm": {
                k: guard_calls[k] for k in ("model", "calls", "input_tokens", "output_tokens")
            },
            "timings_ms": {
                "guarded_total": total_ms,
                "generation": generation_ms,
                "guard": max(0, total_ms - generation_ms),
            },
            "job_result": decision_to_job_result(report, draft),
            "report": json.loads(report_json(report)),
        }
        return CaseExecution(record=record, guard_errors=guard_errors)
