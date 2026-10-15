"""MailGuardPipeline - orchestrates the guard around the core reply agent.

    inbound:   email -> L1 Inbound Guard -> L3 gate pass 1 -> L2 Doc Scanner -> L3 gate pass 2
    prompt:    kept chunks + intent -> channel isolation -> SecurePrompt
    outbound:  draft -> L4 Output Guard (scan + policy stage outbound + inbound floor)

The report is schema 3 (``inbound_guard``, ``doc_scanner``, ``isolation``, ``output_guard``)
and ``GuardConfig`` uses the v3 switches.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from mailguard.config.settings import MailGuardSettings, get_settings
from mailguard.contracts.email import DraftCandidate, GuardedEmail, SourceDocument
from mailguard.contracts.policy import GuardReport, PolicyAction, PolicyDecision
from mailguard.contracts.verdict import InboundVerdict, LayerVerdict, Severity
from mailguard.layers.doc_scanner.scanner import DocScanner
from mailguard.layers.inbound_gate.gate import InboundGate
from mailguard.layers.inbound_guard.guard import InboundGuard
from mailguard.layers.output_guard.guard import OutputGuard
from mailguard.layers.output_guard.scanner import OutputScanner
from mailguard.llm.judge_calls import (
    MAX_GUARD_CALLS_PER_JOB,
    counting_guard_calls,
    merge_guard_calls,
)
from mailguard.llm.protocol import ChatMessage, LLMProvider
from mailguard.llm.registry import ModelRegistry
from mailguard.policy.engine import PolicyEngine
from mailguard.prompting.channel_isolation import ChannelIsolation, SecurePrompt
from mailguard.prompting.questions import question_sentences

logger = logging.getLogger(__name__)

ESCALATED_BODY = (
    "Thank you for your message. It needs a review by our team before we can reply; "
    "we will get back to you."
)
"""Fixed body of ``build_escalated_draft`` (never generated, never quotes the email)."""

DraftFactory = Callable[[list[ChatMessage]], Awaitable[DraftCandidate | dict[str, Any] | str]]


@dataclass(frozen=True)
class GuardConfig:
    """Which guard layers are active (ADR-0015, schema 3).

    The four layers, in pipeline order: ``inbound_guard`` (L1), ``doc_scanner`` (L2),
    ``inbound_gate`` (L3), ``output_guard`` (L4). ``channel_isolation`` is a separate switch,
    not a layer (R-d): off, the prompt is plain concatenation (the no-defence baseline).

    Presets: ``C0`` nothing; ``C1`` L1 + L3; ``C2`` C1 + L2 + isolation; ``C3`` everything;
    ablations ``C3-L1`` .. ``C3-L4`` (C3 minus one layer) and ``C3-ISO`` (C3 without isolation).
    The v2 names ``C3-L3B`` and ``C3-L5`` no longer exist.

    ``inbound_gate`` makes the decision before generation; ``output_guard`` makes the one
    after it (outbound policy merged with the inbound floor). Off, the Output Guard leaves
    ``report.decision`` at the inbound gate's decision.
    """

    name: str = "C3"
    inbound_guard: bool = True
    doc_scanner: bool = True
    inbound_gate: bool = True
    output_guard: bool = True
    channel_isolation: bool = True

    LAYERS: ClassVar[tuple[str, ...]] = (
        "inbound_guard",
        "doc_scanner",
        "inbound_gate",
        "output_guard",
    )
    PRESETS: ClassVar[tuple[str, ...]] = ("C0", "C1", "C2", "C3")
    ABLATIONS: ClassVar[dict[str, str]] = {
        "C3-L1": "inbound_guard",
        "C3-L2": "doc_scanner",
        "C3-L3": "inbound_gate",
        "C3-L4": "output_guard",
        "C3-ISO": "channel_isolation",
    }

    @classmethod
    def preset(cls, name: str) -> GuardConfig:
        key = name.upper().replace(" ", "")
        if key == "C0":
            return cls("C0", False, False, False, False, False)
        if key == "C1":
            return cls("C1", inbound_guard=True, doc_scanner=False, inbound_gate=True,
                       output_guard=False, channel_isolation=False)  # fmt: skip
        if key == "C2":
            return cls("C2", inbound_guard=True, doc_scanner=True, inbound_gate=True,
                       output_guard=False, channel_isolation=True)  # fmt: skip
        if key == "C3":
            return cls("C3")
        if key in cls.ABLATIONS:
            return replace(cls("C3"), name=key, **{cls.ABLATIONS[key]: False})
        known = ", ".join((*cls.PRESETS, *cls.ABLATIONS))
        raise ValueError(f"unknown preset {name!r}; the v3 presets are {known}")

    @property
    def active_layers(self) -> list[str]:
        """The active layers among the four, in pipeline order (isolation is not one)."""
        return [layer for layer in self.LAYERS if getattr(self, layer)]


@dataclass
class PromptBundle:
    prompt: SecurePrompt
    kept_chunks: list[SourceDocument]
    report: GuardReport

    @property
    def messages(self) -> list[ChatMessage]:
        return self.prompt.messages


class MailGuardPipeline:
    def __init__(
        self,
        settings: MailGuardSettings | None = None,
        config: GuardConfig | None = None,
        *,
        registry: ModelRegistry | None = None,
        inbound_judge: LLMProvider | None = None,
        inbound_extractor: LLMProvider | None = None,
        doc_judge: LLMProvider | None = None,
        doc_set_judge: LLMProvider | None = None,
        output_judge: LLMProvider | None = None,
        inbound_guard: InboundGuard | None = None,
        channel_isolation: ChannelIsolation | None = None,
        doc_scanner: DocScanner | None = None,
        output_guard: OutputGuard | None = None,
        policy: PolicyEngine | None = None,
        audit: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.config = config or GuardConfig()
        self.registry = registry
        gm = self.settings.guard_models
        self.inbound_guard = inbound_guard or InboundGuard(
            self.settings,
            judge=inbound_judge or self._provider(gm.inbound_judge),
            judge_name=gm.inbound_judge,
            extractor_llm=inbound_extractor or self._provider(gm.inbound_extractor),
        )
        self.channel_isolation = channel_isolation or ChannelIsolation(
            self.settings, enabled=self.config.channel_isolation
        )
        self.doc_scanner = doc_scanner or DocScanner(
            self.settings,
            rules=self.inbound_guard.rules,
            classifier=self.inbound_guard.classifier,
            llm=doc_judge
            or (self._provider(gm.doc_judge) if self.settings.doc_scanner.llm_enabled else None),
            set_judge_llm=doc_set_judge
            or (
                self._provider(gm.doc_set_judge)
                if self.settings.doc_scanner.set_judge_llm_enabled
                else None
            ),
        )
        self.policy = policy or PolicyEngine(self.settings, audit=audit)
        self.inbound_gate = InboundGate(self.policy)
        self.output_guard = output_guard or OutputGuard(
            self.settings,
            scanner=OutputScanner(
                self.settings,
                llm=output_judge
                or (
                    self._provider(gm.output_judge)
                    if self.settings.output_guard.llm_enabled
                    else None
                ),
            ),
            policy=self.policy,
        )

    def _provider(self, name: str) -> LLMProvider | None:
        if name in ("", "none", "fake") and self.registry is None:
            return None
        try:
            self.registry = self.registry or ModelRegistry(self.settings)
            return self.registry.get(name)
        except Exception as exc:  # unknown model / missing backend: degrade to cheap stages
            logger.warning("guard model %r unavailable (%s); LLM stage disabled", name, exc)
            return None

    # ------------------------------------------------------------------ inbound
    async def inspect_inbound(
        self,
        email_like: Any,
        chunks: Sequence[Any] = (),
        *,
        category: str | None = None,
        query: str | None = None,
    ) -> GuardReport:
        """L1 Inbound Guard -> L3 gate pass 1 -> L2 Doc Scanner -> L3 gate pass 2.

        Pass 1 sees the Inbound Guard's facts only. A block or quarantine there stops the job:
        no document is scanned and no doc judge is called (``report.doc_scanner`` stays None).
        Otherwise the Doc Scanner scans ``chunks`` (echo reference: the email's question
        sentences, else ``query`` or subject + sanitized body) and pass 2 decides on every
        fact. ``report.inbound_decision`` is the final pass; ``metadata["gate_passes"]`` says
        which. Only the final pass is audited. ``report.metadata["guard_calls"]`` counts the
        judge calls per stage (decision 12).
        """
        with counting_guard_calls() as calls:
            report = await self._inspect_inbound(email_like, chunks, category=category, query=query)
        _record_guard_calls(report, calls)
        return report

    async def _inspect_inbound(
        self,
        email_like: Any,
        chunks: Sequence[Any],
        *,
        category: str | None,
        query: str | None,
    ) -> GuardReport:
        email = GuardedEmail.from_any(email_like)
        report = GuardReport(message_id=email.message_id, organization_id=email.organization_id)
        category = category or email.category
        if self.config.inbound_guard:
            report.inbound_guard = await self.inbound_guard.inspect(email)
        self._record_questions(report, email)
        docs = [SourceDocument.from_any(c) for c in chunks]
        scan = self.config.doc_scanner and bool(docs)
        if self.config.inbound_gate:
            first = self.inbound_gate.decide(report, category=category, gate_pass=1, final=not scan)
            if not scan or self.inbound_gate.stops(first):
                if scan:  # the provisional pass stopped the job: it is the final decision now
                    first = self.inbound_gate.decide(
                        report, category=category, gate_pass=1, final=True
                    )
                report.inbound_decision = report.decision = first
                report.total_latency_ms = _latency(report)
                return report
        if scan:
            report.doc_scanner = await self.doc_scanner.scan(
                docs,
                question_sentences=self.question_sentences_of(report),
                query=self._echo_query(report, email, query),
            )
        if self.config.inbound_gate:
            report.inbound_decision = report.decision = self.inbound_gate.decide(
                report, category=category, gate_pass=2, final=True
            )
        report.total_latency_ms = _latency(report)
        return report

    def _echo_query(self, report: GuardReport, email: GuardedEmail, query: str | None) -> str:
        """The caller's retrieval query, else the customer's own words (never the paraphrase)."""
        intent = self._intent(report)
        body = intent.sanitized_body if (intent and intent.sanitized_body) else email.text
        return query or f"{email.subject}\n{body}"[:1500]

    def build_escalated_draft(self, report: GuardReport) -> DraftCandidate:
        """The holding draft a host files when the inbound gate stopped the job (no model call).

        ``action="escalate"``; the body never quotes the email; the subject names the rule and
        the reason that stopped it. ``run()`` does not return it.
        """
        decision = report.inbound_decision
        reason = decision.reasons[0] if decision is not None and decision.reasons else ""
        rule = decision.matched_rule_id if decision is not None else "none"
        return DraftCandidate(
            message_id=report.message_id,
            organization_id=report.organization_id,
            action="escalate",
            subject=f"[needs review] {rule}: {reason}"[:200],
            body=ESCALATED_BODY,
            category=decision.metadata.get("category") if decision is not None else None,
        )

    @staticmethod
    def _record_questions(report: GuardReport, email: GuardedEmail) -> None:
        """Always extract the email's questions (pure function, not part of L1; decision 8).

        On the sanitized body when the extract sub-stage produced one, else on the raw body.
        Stored on the Inbound Guard verdict when L1 ran, else in ``report.metadata``, so the
        Doc Scanner (Task 7) gets the same input with L1 on and off.
        """
        inbound = report.inbound_guard
        extracted = inbound is not None and inbound.stage("extract") is not None
        body = inbound.sanitized_body if (inbound is not None and extracted) else email.text
        questions = question_sentences(body)
        if inbound is not None:
            inbound.question_sentences = questions
        else:
            report.metadata["question_sentences"] = questions

    @staticmethod
    def question_sentences_of(report: GuardReport) -> list[str]:
        """The questions ``inspect_inbound`` recorded, wherever they were stored."""
        if report.inbound_guard is not None:
            return list(report.inbound_guard.question_sentences)
        return list(report.metadata.get("question_sentences") or [])

    @staticmethod
    def blocked(decision: PolicyDecision | None) -> bool:
        return decision is not None and decision.action in (
            PolicyAction.BLOCK,
            PolicyAction.QUARANTINE,
        )

    # ------------------------------------------------------------------ prompt
    async def build_prompt(
        self,
        report: GuardReport,
        email_like: Any,
        chunks: Sequence[Any] = (),
        *,
        system_instructions: str,
        category_instructions: str = "",
        thread_summary: str | None = None,
        recent_messages: Sequence[str] = (),
        business_data: dict[str, Any] | None = None,
        task_instructions: str | None = None,
        query: str | None = None,
    ) -> PromptBundle:
        """Assemble the guarded prompt from the chunks the Doc Scanner kept (it scans nothing).

        ``chunks`` are the retrieved chunks passed to ``inspect_inbound``; each kept chunk is
        rendered with its trust. ``query`` is accepted for backward compatibility and unused.
        """
        del query
        email = GuardedEmail.from_any(email_like)
        retrieved = [SourceDocument.from_any(c) for c in chunks]
        docs = report.doc_scanner
        if docs is not None:
            keep = set(docs.kept_chunk_ids)
            kept = [c for c in retrieved if c.chunk_id in keep]
            trust = {c.chunk_id: c.trust for c in docs.chunks}
        else:
            kept, trust = retrieved, {}
        prompt, isolation = self.channel_isolation.build(
            system_instructions=system_instructions,
            email=email,
            intent=self._intent(report),
            chunks=kept,
            chunk_trust=trust,
            category_instructions=category_instructions,
            thread_summary=thread_summary,
            recent_messages=recent_messages,
            business_data=business_data,
            task_instructions=task_instructions,
            use_sanitized_body=self.config.inbound_guard,
        )
        if self.config.channel_isolation:
            isolation.chunks_in = len(retrieved)
            report.isolation = isolation
        report.total_latency_ms = _latency(report)
        return PromptBundle(prompt=prompt, kept_chunks=kept, report=report)

    def _intent(self, report: GuardReport) -> InboundVerdict | None:
        """The Inbound Guard verdict carries the intent and sanitized body (None when off)."""
        return report.inbound_guard if self.config.inbound_guard else None

    # ------------------------------------------------------------------ outbound
    async def inspect_outbound(
        self,
        report: GuardReport,
        draft_like: Any,
        *,
        email_like: Any,
        kept_chunks: Sequence[SourceDocument] = (),
        protected_texts: Sequence[str] = (),
        category: str | None = None,
    ) -> GuardReport:
        """L4 Output Guard: scan the draft, decide outbound, carry the inbound floor.

        ``report.decision`` becomes the merged decision. With the Output Guard off there is no
        decision after generation: ``report.decision`` stays the inbound gate's (or None).
        """
        email = GuardedEmail.from_any(email_like)
        draft = DraftCandidate.from_any(draft_like)
        if self.config.output_guard:
            with counting_guard_calls() as calls:
                report.output_guard, report.decision = await self.output_guard.inspect(
                    report,
                    draft,
                    email=email,
                    intent=self._intent(report),
                    kept_chunks=kept_chunks,
                    protected_texts=protected_texts,
                    category=category or draft.category or email.category,
                )
            _record_guard_calls(report, calls)
        report.total_latency_ms = _latency(report)
        return report

    # ------------------------------------------------------------------ end to end
    async def run(
        self,
        email_like: Any,
        chunks: Sequence[Any],
        generate: DraftFactory,
        *,
        system_instructions: str,
        category_instructions: str = "",
        thread_summary: str | None = None,
        recent_messages: Sequence[str] = (),
        business_data: dict[str, Any] | None = None,
        task_instructions: str | None = None,
        category: str | None = None,
        query: str | None = None,
        stop_on_inbound_block: bool = True,
    ) -> tuple[GuardReport, DraftCandidate | None, PromptBundle | None]:
        """Full path. Returns (report, guarded draft or None, prompt bundle or None).

        ``query`` is the retrieval query the core used to fetch ``chunks`` (doc-scanner echo check);
        when omitted the customer's own subject + body is used.
        """
        email = GuardedEmail.from_any(email_like)
        report = await self.inspect_inbound(email, chunks, category=category, query=query)
        if stop_on_inbound_block and self.blocked(report.inbound_decision):
            return report, None, None
        bundle = await self.build_prompt(
            report,
            email,
            chunks,
            system_instructions=system_instructions,
            category_instructions=category_instructions,
            thread_summary=thread_summary,
            recent_messages=recent_messages,
            business_data=business_data,
            task_instructions=task_instructions,
            query=query,
        )
        raw = await generate(bundle.messages)
        draft = (
            raw
            if isinstance(raw, DraftCandidate)
            else (
                DraftCandidate(body=raw) if isinstance(raw, str) else DraftCandidate.from_any(raw)
            )
        )
        draft.message_id = draft.message_id or email.message_id
        draft.organization_id = draft.organization_id or email.organization_id
        report = await self.inspect_outbound(
            report,
            draft,
            email_like=email,
            kept_chunks=bundle.kept_chunks,
            protected_texts=[system_instructions, category_instructions],
            category=category,
        )
        if report.output_guard is not None and report.output_guard.redacted_text != draft.body:
            draft = draft.model_copy(update={"body": report.output_guard.redacted_text})
        return report, draft, bundle

    # ------------------------------------------------------------------ helpers
    def summary(self, report: GuardReport) -> dict[str, Any]:
        d = report.decision
        return {
            "config": self.config.name,
            "schema_version": report.schema_version,
            "message_id": report.message_id,
            "max_severity": str(report.max_severity),
            "action": str(d.action) if d else None,
            "rule": d.matched_rule_id if d else None,
            "risk_tier": str(d.risk_tier) if d else None,
            "layers": {
                str(v.layer): {
                    "severity": str(v.severity),
                    "score": v.score,
                    "latency_ms": v.latency_ms,
                }
                for v in report.verdicts()
            },
            "doc_scanner_quarantined": report.quarantined_chunk_ids,
            "total_latency_ms": report.total_latency_ms,
        }


def _latency(report: GuardReport) -> int:
    isolation = report.isolation.latency_ms if report.isolation is not None else 0
    return sum(v.latency_ms for v in report.verdicts()) + isolation


def _record_guard_calls(report: GuardReport, calls: Counter[str]) -> None:
    """Add one step's judge calls to ``report.metadata["guard_calls"]`` ({stage: count})."""
    report.metadata["guard_calls"] = merge_guard_calls(report.metadata.get("guard_calls"), calls)
    total = sum(report.metadata["guard_calls"].values())
    if total > MAX_GUARD_CALLS_PER_JOB:
        logger.warning(
            "guard used %d model calls on %s, above the budget of %d",
            total,
            report.message_id or "a job",
            MAX_GUARD_CALLS_PER_JOB,
        )


def severity_of(report: GuardReport) -> Severity:
    return report.max_severity


def verdict_or_none(v: LayerVerdict | None) -> LayerVerdict | None:
    return v


__all__ = ["DraftFactory", "GuardConfig", "MailGuardPipeline", "PromptBundle"]
