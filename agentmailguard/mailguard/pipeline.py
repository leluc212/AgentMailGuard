"""MailGuardPipeline - orchestrates the guard around the core reply agent.

    inbound:   email  -> scan + extract -> InboundVerdict -> policy (stage inbound)
    prompt:    chunks -> doc scanner -> DocumentVerdict -> channel isolation -> SecurePrompt
    outbound:  draft  -> output scanner -> policy (stage outbound)

The report is schema 3 (``inbound_guard``, ``doc_scanner``, ``isolation``, ``output_guard``)
and ``GuardConfig`` uses the v3 switches. The ordering of the stages is v2's until Task 7
moves the doc scanner before the gate.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from mailguard.config.settings import MailGuardSettings, get_settings
from mailguard.contracts.email import DraftCandidate, GuardedEmail, SourceDocument
from mailguard.contracts.policy import GuardReport, PolicyAction, PolicyDecision
from mailguard.contracts.verdict import DocumentVerdict, InboundVerdict, LayerVerdict, Severity
from mailguard.layers.doc_scanner.chunk_scanner import RetrievedDocumentScanner
from mailguard.layers.inbound_guard.guard import InboundGuard
from mailguard.layers.output_guard.scanner import OutputScanner
from mailguard.llm.protocol import ChatMessage, LLMProvider
from mailguard.llm.registry import ModelRegistry
from mailguard.policy.engine import PolicyEngine
from mailguard.prompting.channel_isolation import ChannelIsolation, SecurePrompt
from mailguard.prompting.questions import question_sentences

logger = logging.getLogger(__name__)

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

    Until Task 8 the outbound policy decision follows ``inbound_gate`` (the v2 ``l5`` switch
    made both decisions); Task 8 moves it into the Output Guard (plan_v3 F12).
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
        output_judge: LLMProvider | None = None,
        inbound_guard: InboundGuard | None = None,
        channel_isolation: ChannelIsolation | None = None,
        doc_scanner: RetrievedDocumentScanner | None = None,
        output_guard: OutputScanner | None = None,
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
        self.doc_scanner = doc_scanner or RetrievedDocumentScanner(
            self.settings,
            rules=self.inbound_guard.rules,
            classifier=self.inbound_guard.classifier,
            llm=doc_judge
            or (self._provider(gm.doc_judge) if self.settings.doc_scanner.llm_enabled else None),
        )
        self.output_guard = output_guard or OutputScanner(
            self.settings,
            llm=output_judge
            or (
                self._provider(gm.output_judge) if self.settings.output_guard.llm_enabled else None
            ),
        )
        self.policy = policy or PolicyEngine(self.settings, audit=audit)

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
    async def inspect_inbound(self, email_like: Any, *, category: str | None = None) -> GuardReport:
        email = GuardedEmail.from_any(email_like)
        report = GuardReport(message_id=email.message_id, organization_id=email.organization_id)
        if self.config.inbound_guard:
            report.inbound_guard = await self.inbound_guard.inspect(email)
        self._record_questions(report, email)
        report.total_latency_ms = _latency(report)
        if self.config.inbound_gate:
            report.inbound_decision = self.policy.decide(
                report, stage="inbound", category=category or email.category
            )
            report.decision = report.inbound_decision
        return report

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
        email = GuardedEmail.from_any(email_like)
        retrieved = [SourceDocument.from_any(c) for c in chunks]
        kept = retrieved
        intent = self._intent(report)
        if self.config.doc_scanner:
            # The retrieval query is what PoisonedRAG bait echoes: use the caller's query
            # (core query builder) or fall back to the customer's own words, never the
            # extractor's paraphrase (its extra tokens dilute the echo measurement).
            body = intent.sanitized_body if (intent and intent.sanitized_body) else email.text
            scan_query = query or f"{email.subject}\n{body}"[:1500]
            kept, verdicts = await self.doc_scanner.scan(retrieved, scan_query)
            report.doc_scanner = DocumentVerdict.from_chunks(verdicts)
        prompt, isolation = self.channel_isolation.build(
            system_instructions=system_instructions,
            email=email,
            intent=intent,
            chunks=kept,
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
        email = GuardedEmail.from_any(email_like)
        draft = DraftCandidate.from_any(draft_like)
        if self.config.output_guard:
            indicators: dict[str, list[str]] = {"emails": [], "urls": []}
            instructions: list[str] = []
            inbound = report.inbound_guard
            scan = (inbound.stage("scan") if inbound is not None else None) or {}
            extract = (inbound.stage("extract") if inbound is not None else None) or {}
            if scan:
                scan_meta = scan.get("metadata") or {}
                ind = scan_meta.get("indicators") or {}
                indicators = {
                    "emails": list(ind.get("emails", [])),
                    "urls": list(ind.get("urls", [])),
                }
                instructions += list(scan_meta.get("injected_instructions", []))
            if inbound is not None:
                for f in inbound.findings:
                    if f.metadata.get("stage") == "scan":
                        instructions += list(f.metadata.get("injected_instructions", []))
            if extract and inbound is not None:
                instructions += [s.text for s in inbound.stripped_segments]
                extract_meta = extract.get("metadata") or {}
                instructions += list(extract_meta.get("instructions_to_assistant", []))
            # payload found inside quarantined knowledge must not surface either
            if report.doc_scanner is not None:
                for cv in report.doc_scanner.chunks:
                    if cv.quarantined:
                        for f in cv.findings:
                            if f.excerpt:
                                instructions.append(f.excerpt)
            report.output_guard = await self.output_guard.inspect(
                draft,
                email=email,
                intent=self._intent(report),
                allowed_citations={c.citation_id for c in kept_chunks}
                | {c.chunk_id for c in kept_chunks},
                protected_texts=protected_texts,
                trusted_texts=[c.content for c in kept_chunks],
                injected_indicators=indicators,
                injected_instructions=list(dict.fromkeys(i for i in instructions if i))[:20],
            )
        report.total_latency_ms = _latency(report)
        # transitional (F12): the outbound decision follows inbound_gate until Task 8
        if self.config.inbound_gate:
            report.decision = self.policy.decide(
                report,
                stage="outbound",
                category=category or draft.category or email.category,
                draft_action=draft.action,
            )
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
        report = await self.inspect_inbound(email, category=category)
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


def severity_of(report: GuardReport) -> Severity:
    return report.max_severity


def verdict_or_none(v: LayerVerdict | None) -> LayerVerdict | None:
    return v


__all__ = ["DraftFactory", "GuardConfig", "MailGuardPipeline", "PromptBundle"]
