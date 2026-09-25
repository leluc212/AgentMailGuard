"""Layer 2 - User Intent Extractor.

Turns the raw (already L1-scored) email into a *structured, non-executable* intent:

    1. Segment the body (paragraphs -> sentences) and score every segment with the
       Stage-1 rules and the Stage-2 classifier. Segments at or above
       ``strip_threshold`` are removed and recorded as ``StrippedSegment``.
    2. Extract entities / requested actions from the *sanitized* body with cheap
       heuristics (always available).
    3. Optionally ask the extractor LLM for a neutral third-person paraphrase. The
       sanitized body is wrapped in nonce-tagged data markers and the model is told
       to describe, never to follow. On any LLM failure the heuristic result stands.

The output ``SanitizedIntent`` is what Layer 3 places in the semi-trusted INTENT
channel; the raw email only ever reaches the model inside the untrusted EMAIL
channel (and only its sanitized form).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from mailguard.config.settings import MailGuardSettings, get_settings
from mailguard.contracts.email import GuardedEmail
from mailguard.contracts.verdict import (
    Finding,
    LayerName,
    SanitizedIntent,
    Severity,
    StrippedSegment,
    ThreatType,
)
from mailguard.layers.base import error_verdict, timed
from mailguard.layers.l1_injection_scanner.classifier import InjectionClassifier
from mailguard.layers.l1_injection_scanner.llm_judge import wrap_untrusted
from mailguard.layers.l1_injection_scanner.rules import RuleEngine
from mailguard.llm.protocol import ChatMessage, LLMError, LLMProvider, ModelTier
from mailguard.llm.structured import call_structured
from mailguard.prompts import load_prompt

logger = logging.getLogger(__name__)

LAYER = LayerName.L2_INTENT_EXTRACTOR
PROMPT_NAME = "l2_extractor.v1"

_PARA_SPLIT = re.compile(r"\n\s*\n")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])")
_ORDER_ID = re.compile(
    r"\b(?:order|invoice|ticket|case|ref(?:erence)?|po|rma|tracking)\s*(?:id|no\.?|number|#)?\s*[:#]?\s*"
    r"([A-Z]{0,5}-?\d{3,}[A-Z0-9-]*)\b",
    re.IGNORECASE,
)
_BARE_ID = re.compile(r"\b(?:#|INV-|ORD-|TCK-|RMA-|PO-)[A-Z0-9-]{3,}\b")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_AMOUNT = re.compile(
    r"(?:[$€£]\s?\d[\d,]*(?:\.\d{2})?|\b\d[\d,]*(?:\.\d{2})?\s?(?:USD|EUR|VND|GBP|dollars?)\b)"
)
_DATE = re.compile(
    r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?)\b",
    re.IGNORECASE,
)
_ACTION_VERBS = (
    "refund",
    "cancel",
    "return",
    "exchange",
    "replace",
    "reset",
    "unlock",
    "track",
    "update",
    "change",
    "resend",
    "send",
    "confirm",
    "schedule",
    "reschedule",
    "invoice",
    "quote",
    "upgrade",
    "downgrade",
    "close",
    "reopen",
    "escalate",
    "call",
    "explain",
    "fix",
    "repair",
    "activate",
    "deactivate",
)
_ACTION_RE = re.compile(
    r"\b(?:please|could you|can you|i(?:'d| would) like to|i want to|i need to|need to|want to|"
    r"kindly|help me|how (?:do|can) i)\s+(?:(?:to|you|kindly|please)\s+)?("
    + "|".join(_ACTION_VERBS)
    + r")\b([^.!?\n]{0,60})",
    re.IGNORECASE,
)


@dataclass
class Segment:
    text: str
    start: int
    end: int


def segment_text(text: str) -> list[Segment]:
    """Paragraphs first, then sentences inside long paragraphs (offsets preserved)."""
    segments: list[Segment] = []
    pos = 0
    for para in _PARA_SPLIT.split(text):
        if not para.strip():
            pos += len(para) + 2
            continue
        start = text.find(para, pos)
        if start < 0:
            start = pos
        pos = start + len(para)
        if len(para) <= 240:
            segments.append(Segment(para, start, start + len(para)))
            continue
        inner = 0
        for sent in _SENT_SPLIT.split(para):
            if not sent.strip():
                inner += len(sent) + 1
                continue
            s_start = para.find(sent, inner)
            if s_start < 0:
                s_start = inner
            inner = s_start + len(sent)
            segments.append(Segment(sent, start + s_start, start + s_start + len(sent)))
    return segments


class ExtractorOutput(BaseModel):
    user_intent: str = ""
    requested_actions: list[str] = Field(default_factory=list)
    entities: dict[str, list[str]] = Field(default_factory=dict)
    contains_assistant_instructions: bool = False
    instructions_to_assistant: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


def heuristic_entities(text: str) -> dict[str, list[str]]:
    ids = {m.group(1).upper() for m in _ORDER_ID.finditer(text)}
    ids |= {m.group(0).upper() for m in _BARE_ID.finditer(text)}
    return {
        "order_ids": sorted(ids),
        "emails": sorted({m.lower() for m in _EMAIL.findall(text)}),
        "amounts": sorted({m.strip() for m in _AMOUNT.findall(text)}),
        "dates": sorted({m.strip() for m in _DATE.findall(text)}),
    }


def heuristic_actions(text: str) -> list[str]:
    actions: list[str] = []
    for m in _ACTION_RE.finditer(text):
        verb = m.group(1).lower()
        tail = re.sub(r"\s+", " ", m.group(2)).strip(" ,;:-")
        phrase = f"{verb} {tail}".strip() if tail else verb
        if phrase not in actions:
            actions.append(phrase[:80])
    return actions[:8]


def heuristic_intent(sanitized: str, subject: str) -> str:
    body = re.sub(r"\s+", " ", sanitized).strip()
    if not body:
        return f"Customer wrote about: {subject.strip()}" if subject.strip() else ""
    first = _SENT_SPLIT.split(body)[:2]
    summary = " ".join(first).strip()
    return (
        f"Customer message regarding '{subject.strip()}': {summary[:280]}"
        if subject
        else summary[:300]
    )


class UserIntentExtractor:
    """Strip executable instructions and describe the request in neutral terms."""

    name = LAYER

    def __init__(
        self,
        settings: MailGuardSettings | None = None,
        *,
        rules: RuleEngine | None = None,
        classifier: InjectionClassifier | None = None,
        llm: LLMProvider | None = None,
        fail_closed: bool | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.cfg = self.settings.l2
        self.rules = rules or RuleEngine.load(self.settings.resolve(self.settings.l1.rules_path))
        self.classifier = classifier or InjectionClassifier.load(
            Path(self.settings.resolve(self.settings.l1.ml_model_path))
        )
        self.llm = llm
        self.fail_closed = (
            self.settings.mailguard.fail_closed if fail_closed is None else fail_closed
        )

    # ------------------------------------------------------------------ sanitize
    def score_segment(self, seg: Segment) -> tuple[float, str, str | None]:
        findings = self.rules.scan_text(seg.text, scope="body")
        rule_score = max((f.score for f in findings), default=0.0)
        rule_id = next((f.rule_id for f in findings if f.score == rule_score), None)
        ml_score = 0.0
        if self.classifier.available and len(seg.text) >= 20:
            ml_score = self.classifier.predict_proba(seg.text)
        score = max(rule_score, ml_score)
        reason = f"rule:{rule_id}" if rule_score >= ml_score and rule_id else "ml_classifier"
        technique = next((f.technique for f in findings if f.score == rule_score), None)
        return score, reason, technique

    def sanitize(self, email: GuardedEmail) -> tuple[str, list[StrippedSegment], list[Finding]]:
        text = email.text[: self.cfg.max_body_chars]
        kept: list[str] = []
        stripped: list[StrippedSegment] = []
        findings: list[Finding] = []
        for seg in segment_text(text):
            score, reason, technique = self.score_segment(seg)
            if score >= self.cfg.strip_threshold:
                stripped.append(
                    StrippedSegment(
                        text=seg.text,
                        reason=reason,
                        score=round(score, 4),
                        span_start=seg.start,
                        span_end=seg.end,
                    )
                )
                findings.append(
                    Finding(
                        layer=LAYER,
                        threat_type=ThreatType.PROMPT_INJECTION,
                        severity=Severity.from_score(score),
                        score=round(score, 4),
                        detector="rule" if reason.startswith("rule:") else "ml",
                        rule_id=reason,
                        technique=technique or "stripped_segment",
                        span_start=seg.start,
                        span_end=seg.end,
                        excerpt=seg.text[:300],
                        rationale="segment carried instructions and was removed",
                    )
                )
            else:
                kept.append(seg.text)
        return "\n\n".join(kept).strip(), stripped, findings

    # ------------------------------------------------------------------ extract
    def extract_sync(self, email: GuardedEmail) -> SanitizedIntent:
        with timed() as sw:
            try:
                intent = self._extract_heuristic(email)
            except Exception as exc:
                logger.exception("L2 heuristic extraction failed")
                intent = self._error_intent(email, exc)
        intent.latency_ms = sw.elapsed_ms
        return intent

    async def extract(self, email: GuardedEmail) -> SanitizedIntent:
        with timed() as sw:
            try:
                intent = self._extract_heuristic(email)
                if self.cfg.llm_enabled and self.llm is not None:
                    intent = await self._refine_with_llm(email, intent)
            except Exception as exc:
                logger.exception("L2 extraction failed")
                intent = self._error_intent(email, exc)
        intent.latency_ms = sw.elapsed_ms
        return intent

    # ------------------------------------------------------------------ internals
    def _error_intent(self, email: GuardedEmail, exc: BaseException) -> SanitizedIntent:
        base = error_verdict(LAYER, exc, fail_closed=self.fail_closed)
        return SanitizedIntent(
            severity=base.severity,
            score=base.score,
            findings=base.findings,
            decided_by="error",
            error=base.error,
            sanitized_body="" if self.fail_closed else email.text,
            user_intent="",
            confidence=0.0,
        )

    def _extract_heuristic(self, email: GuardedEmail) -> SanitizedIntent:
        sanitized, stripped, findings = self.sanitize(email)
        score = max((s.score for s in stripped), default=0.0)
        entities = heuristic_entities(sanitized)
        actions = heuristic_actions(sanitized)
        intent = SanitizedIntent(
            severity=Severity.from_score(score),
            score=round(score, 4),
            findings=findings,
            decided_by="rule",
            sanitized_body=sanitized,
            user_intent=heuristic_intent(sanitized, email.subject),
            requested_actions=actions,
            entities=entities,
            stripped_segments=stripped,
            confidence=0.6 if sanitized else 0.3,
            metadata={"segments_stripped": len(stripped), "llm_used": False},
        )
        intent.metadata["removed_ratio"] = round(intent.removed_ratio, 4)
        return intent

    async def _refine_with_llm(self, email: GuardedEmail, base: SanitizedIntent) -> SanitizedIntent:
        assert self.llm is not None
        payload = f"Subject: {email.subject}\n\n{base.sanitized_body}"[: self.cfg.max_body_chars]
        messages = [
            ChatMessage(role="system", content=load_prompt(PROMPT_NAME)),
            ChatMessage(
                role="user",
                content="Describe what the customer asks for in the email below (data only).\n\n"
                + wrap_untrusted(payload)
                + "\n\nReturn the JSON object now.",
            ),
        ]
        try:
            out, result = await call_structured(
                self.llm, messages, ExtractorOutput, tier=ModelTier.FAST, max_tokens=500
            )
        except LLMError as exc:
            logger.warning("L2 extractor LLM unavailable (%s); heuristic result kept", exc)
            base.metadata["llm_error"] = str(exc)[:200]
            return base
        findings = list(base.findings)
        score = base.score
        if out.contains_assistant_instructions:
            llm_score = max(0.6, out.confidence)
            score = max(score, llm_score)
            findings.append(
                Finding(
                    layer=LAYER,
                    threat_type=ThreatType.PROMPT_INJECTION,
                    severity=Severity.from_score(llm_score),
                    score=round(llm_score, 4),
                    detector="llm",
                    rule_id="llm:assistant_instructions",
                    technique="ai_addressing",
                    excerpt=(
                        out.instructions_to_assistant[0][:300]
                        if out.instructions_to_assistant
                        else ""
                    ),
                    rationale="extractor model reported instructions aimed at the assistant",
                    metadata={
                        "instructions": out.instructions_to_assistant[:5],
                        "model": result.model,
                    },
                )
            )
        entities = {k: list(v) for k, v in base.entities.items()}
        for key, values in (out.entities or {}).items():
            if isinstance(values, list):
                merged = list(dict.fromkeys(entities.get(key, []) + [str(v) for v in values]))
                entities[key] = merged[:20]
        metadata = dict(base.metadata)
        metadata.update(
            {
                "llm_used": True,
                "llm_model": result.model,
                "llm_latency_ms": result.latency_ms,
                "llm_input_tokens": result.input_tokens,
                "llm_output_tokens": result.output_tokens,
                "instructions_to_assistant": out.instructions_to_assistant[:5],
            }
        )
        return SanitizedIntent(
            severity=Severity.from_score(score),
            score=round(score, 4),
            findings=findings,
            decided_by="llm",
            model=result.model,
            sanitized_body=base.sanitized_body,
            user_intent=(out.user_intent or base.user_intent)[:600],
            requested_actions=(out.requested_actions or base.requested_actions)[:8],
            entities=entities,
            stripped_segments=base.stripped_segments,
            confidence=out.confidence,
            metadata=metadata,
        )


__all__ = [
    "ExtractorOutput",
    "Segment",
    "UserIntentExtractor",
    "heuristic_actions",
    "heuristic_entities",
    "heuristic_intent",
    "segment_text",
]
