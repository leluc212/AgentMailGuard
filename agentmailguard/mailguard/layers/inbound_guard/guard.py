"""L1 Inbound Guard: one facade over the scan and extract sub-stages (ADR-0015, Q5=a).

    scan     rules + obfuscation -> classifier -> optional LLM judge   (EmailInjectionScanner)
    extract  strip injected segments, intent heuristic -> optional LLM (UserIntentExtractor)

Both sub-stages share one rule engine and one classifier. ``inspect`` returns one
``InboundVerdict``: severity and score come from the scan only (R-i); the extractor adds its
findings, intent and sanitized body but never raises the severity. At most two model calls
per email (scan judge, extractor), each only when its switch is on and a provider is set.
"""

from __future__ import annotations

from mailguard.config.settings import MailGuardSettings, get_settings
from mailguard.contracts.email import GuardedEmail
from mailguard.contracts.verdict import InboundVerdict, LayerName
from mailguard.layers.inbound_guard.classifier import InjectionClassifier
from mailguard.layers.inbound_guard.extract import UserIntentExtractor
from mailguard.layers.inbound_guard.rules import RuleEngine
from mailguard.layers.inbound_guard.scan import EmailInjectionScanner
from mailguard.llm.protocol import LLMProvider


class InboundGuard:
    """L1 Inbound Guard: scan then extract, merged into one ``InboundVerdict``."""

    name = LayerName.INBOUND_GUARD

    def __init__(
        self,
        settings: MailGuardSettings | None = None,
        *,
        judge: LLMProvider | None = None,
        extractor_llm: LLMProvider | None = None,
        judge_name: str | None = None,
        scan: EmailInjectionScanner | None = None,
        extract: UserIntentExtractor | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.scan = scan or EmailInjectionScanner(self.settings, judge=judge, judge_name=judge_name)
        self.extract = extract or UserIntentExtractor(
            self.settings,
            rules=self.scan.rules,
            classifier=self.scan.classifier,
            llm=extractor_llm,
        )

    @property
    def rules(self) -> RuleEngine:
        return self.scan.rules

    @property
    def classifier(self) -> InjectionClassifier:
        return self.scan.classifier

    @property
    def judge(self) -> LLMProvider | None:
        """The scan sub-stage's LLM judge (None: the scan stays cheap)."""
        return self.scan.judge

    @judge.setter
    def judge(self, provider: LLMProvider | None) -> None:
        self.scan.judge = provider

    @property
    def extractor_llm(self) -> LLMProvider | None:
        """The extract sub-stage's LLM (None: the deterministic extraction only)."""
        return self.extract.llm

    @extractor_llm.setter
    def extractor_llm(self, provider: LLMProvider | None) -> None:
        self.extract.llm = provider

    async def inspect(
        self, email: GuardedEmail, *, run_scan: bool = True, run_extract: bool = True
    ) -> InboundVerdict | None:
        """Scan, then extract; ``None`` when neither sub-stage runs.

        ``run_scan`` / ``run_extract`` exist only while ``GuardConfig`` still switches the two
        sub-stages apart (v2 presets); Task 6 makes the Inbound Guard a single switch. The
        extract sub-stage always runs its deterministic part; only its LLM is optional (F4).
        """
        scan = await self.scan.inspect(email) if run_scan else None
        extract = await self.extract.extract(email) if run_extract else None
        return InboundVerdict.merge(scan, extract)


__all__ = ["InboundGuard"]
