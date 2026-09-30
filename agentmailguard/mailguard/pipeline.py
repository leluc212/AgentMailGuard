"""MailGuardPipeline - orchestrates the six guard layers around the core reply agent.

    inbound:   email  -> L1 scanner -> L2 extractor -> L5 (inbound gate)
    prompt:    chunks -> L3b document scanner -> L3 channel isolation -> SecurePrompt
    outbound:  draft  -> L4 output scanner -> L5 (outbound gate)

``GuardConfig`` switches individual layers on and off so the same code path
serves the paper's ablation configurations (C0 none, C1 L1-only, C2 L1+L2+L3,
C3 full, and "C3 minus one layer").
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from mailguard.config.settings import MailGuardSettings, get_settings
from mailguard.contracts.email import DraftCandidate, GuardedEmail, RetrievedChunk
from mailguard.contracts.policy import GuardReport, PolicyAction, PolicyDecision
from mailguard.contracts.verdict import LayerName, LayerVerdict, Severity
from mailguard.layers.l1_injection_scanner.scanner import EmailInjectionScanner
from mailguard.layers.l2_intent_extractor.extractor import UserIntentExtractor
from mailguard.layers.l3_channel_isolation.isolation import ChannelIsolation, SecurePrompt
from mailguard.layers.l3b_document_scanner.scanner import RetrievedDocumentScanner
from mailguard.layers.l4_output_scanner.scanner import OutputScanner
from mailguard.layers.l5_policy_engine.engine import PolicyEngine
from mailguard.llm.protocol import ChatMessage, LLMProvider
from mailguard.llm.registry import ModelRegistry

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
    kept_chunks: list[RetrievedChunk]
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
        judge: LLMProvider | None = None,
        extractor_llm: LLMProvider | None = None,
        doc_llm: LLMProvider | None = None,
        output_llm: LLMProvider | None = None,
        l1: EmailInjectionScanner | None = None,
        l2: UserIntentExtractor | None = None,
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
        self.l1 = l1 or EmailInjectionScanner(
            self.settings, judge=judge or self._provider(gm.judge), judge_name=gm.judge
        )
        self.l2 = l2 or UserIntentExtractor(
            self.settings,
            rules=self.l1.rules,
            classifier=self.l1.classifier,
            llm=extractor_llm or self._provider(gm.extractor),
        )
        self.l3 = l3 or ChannelIsolation(self.settings, enabled=self.config.l3)
        self.l3b = l3b or RetrievedDocumentScanner(
            self.settings,
            rules=self.l1.rules,
            classifier=self.l1.classifier,
            llm=doc_llm
            or (self._provider(gm.doc_scanner) if self.settings.l3b.llm_enabled else None),
        )
        self.l4 = l4 or OutputScanner(
            self.settings,
            llm=output_llm
            or (self._provider(gm.output_judge) if self.settings.l4.llm_enabled else None),
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
        if self.config.l1:
            report.l1 = await self.l1.inspect(email)
        if self.config.l2:
            report.l2 = await self.l2.extract(email)
        report.total_latency_ms = sum(v.latency_ms for v in report.verdicts())
        if self.config.l5:
            report.inbound_decision = self.l5.decide(
                report, stage="inbound", category=category or email.category
            )
            report.decision = report.inbound_decision
        return report

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
        retrieved = [RetrievedChunk.from_any(c) for c in chunks]
        kept = retrieved
        if self.config.l3b:
            # The retrieval query is what PoisonedRAG bait echoes: use the caller's query
            # (core query builder) or fall back to the customer's own words, never the
            # L2 paraphrase (its extra tokens dilute the echo measurement).
            body = (
                report.l2.sanitized_body if (report.l2 and report.l2.sanitized_body) else email.text
            )
            scan_query = query or f"{email.subject}\n{body}"[:1500]
            kept, verdicts = await self.l3b.scan(retrieved, scan_query)
            report.l3b = verdicts
        prompt, l3_verdict = self.l3.build(
            system_instructions=system_instructions,
            email=email,
            intent=report.l2 if self.config.l2 else None,
            chunks=kept,
            category_instructions=category_instructions,
            thread_summary=thread_summary,
            recent_messages=recent_messages,
            business_data=business_data,
            task_instructions=task_instructions,
            use_sanitized_body=self.config.l2,
        )
        if self.config.l3:
            report.l3 = l3_verdict
        report.total_latency_ms = sum(v.latency_ms for v in report.verdicts())
        return PromptBundle(prompt=prompt, kept_chunks=kept, report=report)

    # ------------------------------------------------------------------ outbound
    async def inspect_outbound(
        self,
        report: GuardReport,
        draft_like: Any,
        *,
        email_like: Any,
        kept_chunks: Sequence[RetrievedChunk] = (),
        protected_texts: Sequence[str] = (),
        category: str | None = None,
    ) -> GuardReport:
        email = GuardedEmail.from_any(email_like)
        draft = DraftCandidate.from_any(draft_like)
        if self.config.l4:
            indicators: dict[str, list[str]] = {"emails": [], "urls": []}
            instructions: list[str] = []
            if report.l1 is not None:
                ind = report.l1.metadata.get("indicators") or {}
                indicators = {
                    "emails": list(ind.get("emails", [])),
                    "urls": list(ind.get("urls", [])),
                }
                instructions += list(report.l1.metadata.get("injected_instructions", []))
                for f in report.l1.findings:
                    instructions += list(f.metadata.get("injected_instructions", []))
            if report.l2 is not None:
                instructions += [s.text for s in report.l2.stripped_segments]
                instructions += list(report.l2.metadata.get("instructions_to_assistant", []))
            # payload found inside quarantined knowledge must not surface either
            for cv in report.l3b:
                if cv.quarantined:
                    for f in cv.findings:
                        if f.excerpt:
                            instructions.append(f.excerpt)
            report.l4 = await self.l4.inspect(
                draft,
                email=email,
                intent=report.l2,
                allowed_citations={c.citation_id for c in kept_chunks}
                | {c.chunk_id for c in kept_chunks},
                protected_texts=protected_texts,
                trusted_texts=[c.content for c in kept_chunks],
                injected_indicators=indicators,
                injected_instructions=list(dict.fromkeys(i for i in instructions if i))[:20],
            )
        report.total_latency_ms = sum(v.latency_ms for v in report.verdicts())
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
        if report.l4 is not None and report.l4.redacted_text != draft.body:
            draft = draft.model_copy(update={"body": report.l4.redacted_text})
        return report, draft, bundle

    # ------------------------------------------------------------------ helpers
    def summary(self, report: GuardReport) -> dict[str, Any]:
        d = report.decision
        return {
            "config": self.config.name,
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
                if v.layer is not LayerName.L3B_DOCUMENT_SCANNER
            },
            "l3b_quarantined": [c.chunk_id for c in report.l3b if c.quarantined],
            "total_latency_ms": report.total_latency_ms,
        }


def severity_of(report: GuardReport) -> Severity:
    return report.max_severity


def verdict_or_none(v: LayerVerdict | None) -> LayerVerdict | None:
    return v


__all__ = ["DraftFactory", "GuardConfig", "MailGuardPipeline", "PromptBundle"]
