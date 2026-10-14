"""MailGuardPipeline - orchestrates the guard around the core reply agent.

    inbound:   email  -> scan + extract -> InboundVerdict -> policy (stage inbound)
    prompt:    chunks -> doc scanner -> DocumentVerdict -> channel isolation -> SecurePrompt
    outbound:  draft  -> output scanner -> policy (stage outbound)

The report is schema 3 (``inbound_guard``, ``doc_scanner``, ``isolation``, ``output_guard``).
``GuardConfig`` still uses the v2 switches ``l1``..``l5`` (renamed in Task 6, ADR-0015);
the ordering of the stages is v2's until Task 7 moves the doc scanner before the gate.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

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
    """Which layers are active. Presets reproduce the paper's Table IV / VI."""

    name: str = "C3"
    l1: bool = True
    l2: bool = True
    l3: bool = True
    l3b: bool = True
    l4: bool = True
    l5: bool = True

    PRESETS = ("C0", "C1", "C2", "C3")

    @classmethod
    def preset(cls, name: str) -> GuardConfig:
        key = name.upper().replace(" ", "")
        if key == "C0":
            return cls("C0", False, False, False, False, False, False)
        if key == "C1":
            return cls("C1", True, False, False, False, False, True)
        if key == "C2":
            return cls("C2", True, True, True, False, False, True)
        if key == "C3":
            return cls("C3")
        if key.startswith("C3-"):  # ablation: C3-L1, C3-L2, C3-L3, C3-L3B, C3-L4, C3-L5
            layer = key[3:].lower()
            if layer not in {"l1", "l2", "l3", "l3b", "l4", "l5"}:
                raise ValueError(f"unknown ablation layer {layer}")
            return replace(cls("C3"), name=key, **{layer: False})
        raise ValueError(f"unknown preset {name}")

    @property
    def active_layers(self) -> list[str]:
        return [k for k in ("l1", "l2", "l3", "l3b", "l4", "l5") if getattr(self, k)]


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
        doc_llm: LLMProvider | None = None,
        output_llm: LLMProvider | None = None,
        inbound_guard: InboundGuard | None = None,
        l3: ChannelIsolation | None = None,
        l3b: RetrievedDocumentScanner | None = None,
        l4: OutputScanner | None = None,
        l5: PolicyEngine | None = None,
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
        self.l3 = l3 or ChannelIsolation(self.settings, enabled=self.config.l3)
        self.l3b = l3b or RetrievedDocumentScanner(
            self.settings,
            rules=self.inbound_guard.rules,
            classifier=self.inbound_guard.classifier,
            llm=doc_llm
            or (self._provider(gm.doc_judge) if self.settings.doc_scanner.llm_enabled else None),
        )
        self.l4 = l4 or OutputScanner(
            self.settings,
            llm=output_llm
            or (
                self._provider(gm.output_judge) if self.settings.output_guard.llm_enabled else None
            ),
        )
        self.l5 = l5 or PolicyEngine(self.settings, audit=audit)

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
        report.inbound_guard = await self.inbound_guard.inspect(
            email, run_scan=self.config.l1, run_extract=self.config.l2
        )
        self._record_questions(report, email)
        report.total_latency_ms = _latency(report)
        if self.config.l5:
            report.inbound_decision = self.l5.decide(
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
        if self.config.l3b:
            # The retrieval query is what PoisonedRAG bait echoes: use the caller's query
            # (core query builder) or fall back to the customer's own words, never the
            # extractor's paraphrase (its extra tokens dilute the echo measurement).
            body = intent.sanitized_body if (intent and intent.sanitized_body) else email.text
            scan_query = query or f"{email.subject}\n{body}"[:1500]
            kept, verdicts = await self.l3b.scan(retrieved, scan_query)
            report.doc_scanner = DocumentVerdict.from_chunks(verdicts)
        prompt, isolation = self.l3.build(
            system_instructions=system_instructions,
            email=email,
            intent=intent,
            chunks=kept,
            category_instructions=category_instructions,
            thread_summary=thread_summary,
            recent_messages=recent_messages,
            business_data=business_data,
            task_instructions=task_instructions,
            use_sanitized_body=self.config.l2,
        )
        if self.config.l3:
            isolation.chunks_in = len(retrieved)
            report.isolation = isolation
        report.total_latency_ms = _latency(report)
        return PromptBundle(prompt=prompt, kept_chunks=kept, report=report)

    def _intent(self, report: GuardReport) -> InboundVerdict | None:
        """The extract sub-stage's intent, only when it ran (the scan alone carries none)."""
        return report.inbound_guard if self.config.l2 else None

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
        if self.config.l4:
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
            report.output_guard = await self.l4.inspect(
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
        if self.config.l5:
            report.decision = self.l5.decide(
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

        ``query`` is the retrieval query the core used to fetch ``chunks`` (L3b echo check);
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
