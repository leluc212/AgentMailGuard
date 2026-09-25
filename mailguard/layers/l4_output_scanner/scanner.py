"""Layer 4 - Output Scanner (outbound guard on the generated draft).

Checks, cheapest first:

    1. sensitive-data patterns  (configs/pii_patterns.yaml; Luhn for cards)  -> redaction
    2. system-prompt / context leak  (n-gram overlap with protected texts)
    3. citation integrity            (draft cites only chunks that were retrieved & kept)
    4. injected-goal compliance      (attacker emails/URLs/phrases from L1/L3b reach the draft)
    5. unsafe action                 (forward / recipients outside the conversation)
    6. external link insertion       (URLs not present in trusted knowledge)
    7. optional LLM judge            (uncertain band only)

Redactions are applied from right to left so spans stay valid, and every redacted
value is stored as a SHA-256 hash for the audit trail (never the value itself).
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from mailguard.config.settings import MailGuardSettings, get_settings
from mailguard.contracts.email import DraftCandidate, GuardedEmail
from mailguard.contracts.verdict import (
    Finding,
    LayerName,
    OutputVerdict,
    Redaction,
    SanitizedIntent,
    Severity,
    ThreatType,
)
from mailguard.layers.base import error_verdict, timed
from mailguard.layers.l1_injection_scanner.llm_judge import wrap_untrusted
from mailguard.llm.protocol import ChatMessage, LLMError, LLMProvider, ModelTier
from mailguard.llm.structured import call_structured
from mailguard.prompts import load_prompt

logger = logging.getLogger(__name__)

LAYER = LayerName.L4_OUTPUT_SCANNER
PROMPT_NAME = "l4_output_judge.v1"

_SEVERITY_SCORE = {
    Severity.NONE: 0.0,
    Severity.LOW: 0.3,
    Severity.MEDIUM: 0.6,
    Severity.HIGH: 0.88,
    Severity.CRITICAL: 0.97,
}
_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class SensitivePattern:
    kind: str
    pattern: re.Pattern[str]
    severity: Severity
    redact: bool = True
    validator: str | None = None

    @property
    def threat_type(self) -> ThreatType:
        if self.kind.startswith("secret."):
            return ThreatType.SECRET_LEAK
        if self.kind.startswith("pii."):
            return ThreatType.PII_LEAK
        if self.kind == "leak.system_prompt_phrase":
            return ThreatType.SYSTEM_PROMPT_LEAK
        return ThreatType.CONTEXT_LEAK


def load_patterns(path: Path) -> list[SensitivePattern]:
    if not path.exists():
        logger.warning("pii patterns not found at %s; L4 pattern stage disabled", path)
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out: list[SensitivePattern] = []
    for raw in data.get("patterns", []):
        try:
            out.append(
                SensitivePattern(
                    kind=str(raw["kind"]),
                    pattern=re.compile(str(raw["pattern"]), re.IGNORECASE),
                    severity=Severity(str(raw.get("severity", "medium"))),
                    redact=bool(raw.get("redact", True)),
                    validator=raw.get("validator"),
                )
            )
        except (re.error, KeyError, ValueError) as exc:
            logger.error("skipping invalid pii pattern %s: %s", raw.get("kind"), exc)
    return out


def luhn_ok(digits: str) -> bool:
    ds = [int(c) for c in digits if c.isdigit()]
    if len(ds) < 13:
        return False
    total = 0
    for i, d in enumerate(reversed(ds)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    words = _WORD.findall(text.lower())
    return {tuple(words[i : i + n]) for i in range(0, max(0, len(words) - n + 1))}


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def domain_of(email_or_url: str) -> str:
    v = email_or_url.lower()
    if "@" in v:
        return v.rsplit("@", 1)[-1]
    m = re.match(r"https?://([^/:?#]+)", v)
    return m.group(1) if m else v


class OutputJudgeOutput(BaseModel):
    safe: bool
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    violations: list[str] = Field(default_factory=list)
    rationale: str = ""

    @property
    def unsafe_score(self) -> float:
        return 1.0 - self.confidence if self.safe else self.confidence


class OutputScanner:
    name = LAYER

    def __init__(
        self,
        settings: MailGuardSettings | None = None,
        *,
        patterns: list[SensitivePattern] | None = None,
        llm: LLMProvider | None = None,
        fail_closed: bool | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.cfg = self.settings.l4
        self.patterns = (
            patterns
            if patterns is not None
            else load_patterns(self.settings.resolve(self.cfg.pii_patterns_path))
        )
        self.llm = llm
        self.fail_closed = (
            self.settings.mailguard.fail_closed if fail_closed is None else fail_closed
        )

    # ------------------------------------------------------------------ API
    def inspect_sync(
        self,
        draft: DraftCandidate,
        *,
        email: GuardedEmail | None = None,
        intent: SanitizedIntent | None = None,
        allowed_citations: Iterable[str] | None = None,
        protected_texts: Sequence[str] = (),
        trusted_texts: Sequence[str] = (),
        injected_indicators: dict[str, list[str]] | None = None,
        injected_instructions: Sequence[str] = (),
        allowed_recipients: Iterable[str] | None = None,
    ) -> OutputVerdict:
        with timed() as sw:
            try:
                verdict = self._inspect(
                    draft,
                    email=email,
                    allowed_citations=set(allowed_citations or []),
                    protected_texts=protected_texts,
                    trusted_texts=trusted_texts,
                    injected_indicators=injected_indicators or {},
                    injected_instructions=injected_instructions,
                    allowed_recipients=set(allowed_recipients or []),
                )
            except Exception as exc:
                logger.exception("L4 output scan failed")
                base = error_verdict(LAYER, exc, fail_closed=self.fail_closed)
                verdict = OutputVerdict(
                    severity=base.severity,
                    score=base.score,
                    findings=base.findings,
                    decided_by="error",
                    error=base.error,
                    original_text=draft.body,
                    redacted_text="" if self.fail_closed else draft.body,
                )
        verdict.latency_ms = sw.elapsed_ms
        return verdict

    async def inspect(
        self,
        draft: DraftCandidate,
        *,
        email: GuardedEmail | None = None,
        intent: SanitizedIntent | None = None,
        allowed_citations: Iterable[str] | None = None,
        protected_texts: Sequence[str] = (),
        trusted_texts: Sequence[str] = (),
        injected_indicators: dict[str, list[str]] | None = None,
        injected_instructions: Sequence[str] = (),
        allowed_recipients: Iterable[str] | None = None,
    ) -> OutputVerdict:
        verdict = self.inspect_sync(
            draft,
            email=email,
            intent=intent,
            allowed_citations=allowed_citations,
            protected_texts=protected_texts,
            trusted_texts=trusted_texts,
            injected_indicators=injected_indicators,
            injected_instructions=injected_instructions,
            allowed_recipients=allowed_recipients,
        )
        if (
            self.cfg.llm_enabled
            and self.llm is not None
            and verdict.error is None
            and verdict.severity.rank < Severity.HIGH.rank
            and (injected_instructions or verdict.score >= 0.2)
        ):
            with timed() as sw:
                verdict = await self._stage_llm(draft, verdict, intent, injected_instructions)
            verdict.latency_ms += sw.elapsed_ms
        return verdict

    # ------------------------------------------------------------------ internals
    def _inspect(
        self,
        draft: DraftCandidate,
        *,
        email: GuardedEmail | None,
        allowed_citations: set[str],
        protected_texts: Sequence[str],
        trusted_texts: Sequence[str],
        injected_indicators: dict[str, list[str]],
        injected_instructions: Sequence[str],
        allowed_recipients: set[str],
    ) -> OutputVerdict:
        text = draft.body
        lowered = text.lower()
        findings: list[Finding] = []
        redactions: list[Redaction] = []
        inbound_text = email.full_text.lower() if email is not None else ""
        customer_emails: set[str] = set()
        if email is not None:
            customer_emails = {email.sender_email.lower(), *[r.lower() for r in email.recipients]}
        allowed_recipients = {r.lower() for r in allowed_recipients} | customer_emails
        allowed_domains = {domain_of(r) for r in allowed_recipients if r} | {
            d.lower() for d in self.cfg.external_domain_allowlist
        }

        # 1. sensitive patterns ---------------------------------------------------
        for pat in self.patterns:
            for m in pat.pattern.finditer(text):
                value = m.group(0)
                if pat.validator == "luhn" and not luhn_ok(value):
                    continue
                if pat.kind == "pii.email" and value.lower() in allowed_recipients:
                    continue
                if pat.kind.startswith("pii.") and value.lower() in inbound_text:
                    continue  # the customer supplied it; echoing it back is not disclosure
                score = _SEVERITY_SCORE[pat.severity]
                findings.append(
                    Finding(
                        layer=LAYER,
                        threat_type=pat.threat_type,
                        severity=pat.severity,
                        score=score,
                        detector="rule",
                        rule_id=pat.kind,
                        technique=pat.kind,
                        span_start=m.start(),
                        span_end=m.end(),
                        excerpt=text[max(0, m.start() - 40) : m.end() + 40].replace("\n", " ")[
                            :300
                        ],
                        rationale=f"{pat.kind} matched",
                    )
                )
                if pat.redact and self.cfg.redact_pii:
                    redactions.append(
                        Redaction(
                            kind=pat.kind,
                            replacement=f"[REDACTED:{pat.kind}]",
                            span_start=m.start(),
                            span_end=m.end(),
                            original_hash=sha256(value),
                        )
                    )

        # 2. system prompt / protected context leak ---------------------------------
        n = self.cfg.system_prompt_leak_ngram
        draft_ngrams = ngrams(text, n)
        for i, protected in enumerate(protected_texts):
            if not protected:
                continue
            overlap = draft_ngrams & ngrams(protected, n)
            if overlap:
                sample = " ".join(next(iter(overlap)))
                findings.append(
                    Finding(
                        layer=LAYER,
                        threat_type=ThreatType.SYSTEM_PROMPT_LEAK,
                        severity=Severity.HIGH,
                        score=0.9,
                        detector="heuristic",
                        rule_id="h-ngram-leak",
                        technique="prompt_extraction",
                        excerpt=sample[:300],
                        rationale=f"{len(overlap)} {n}-grams overlap with protected text #{i}",
                        metadata={"overlap": len(overlap)},
                    )
                )

        # 3. citation integrity -----------------------------------------------------
        unknown = sorted(
            {c for c in draft.citations if allowed_citations and c not in allowed_citations}
        )
        citation_mismatch = bool(unknown)
        if citation_mismatch:
            findings.append(
                Finding(
                    layer=LAYER,
                    threat_type=ThreatType.CITATION_HALLUCINATION,
                    severity=Severity.MEDIUM,
                    score=0.6,
                    detector="heuristic",
                    rule_id="h-citation-mismatch",
                    technique="citation_mismatch",
                    rationale=f"draft cites unknown chunks: {unknown[:5]}",
                    metadata={"unknown_citations": unknown},
                )
            )

        # 4. injected-goal compliance -----------------------------------------------
        complied = False
        hits: list[str] = []
        for addr in injected_indicators.get("emails", []):
            if addr.lower() in lowered or addr.lower() in {r.lower() for r in draft.recipients}:
                hits.append(addr)
        for url in injected_indicators.get("urls", []):
            if url.lower() in lowered:
                hits.append(url)
        for instr in injected_instructions:
            grams = ngrams(instr, 6)
            if grams and grams & ngrams(text, 6):
                hits.append(instr[:80])
        if hits:
            complied = True
            findings.append(
                Finding(
                    layer=LAYER,
                    threat_type=ThreatType.INJECTED_GOAL_COMPLIANCE,
                    severity=Severity.HIGH,
                    score=0.95,
                    detector="heuristic",
                    rule_id="h-injected-goal",
                    technique="injected_goal_compliance",
                    excerpt="; ".join(hits)[:300],
                    rationale="draft contains attacker-supplied payload (address / link / phrase)",
                    metadata={"hits": hits[:10]},
                )
            )

        # 5. unsafe action ----------------------------------------------------------
        bad_recipients = [
            r
            for r in draft.recipients
            if r.lower() not in allowed_recipients and domain_of(r) not in allowed_domains
        ]
        if draft.action == "forward" or bad_recipients:
            findings.append(
                Finding(
                    layer=LAYER,
                    threat_type=ThreatType.UNSAFE_ACTION,
                    severity=Severity.HIGH,
                    score=0.9,
                    detector="heuristic",
                    rule_id="h-unsafe-action",
                    technique="exfiltration_email",
                    rationale=(
                        f"action={draft.action} "
                        f"recipients_outside_conversation={bad_recipients[:5]}"
                    ),
                    metadata={"recipients": bad_recipients[:10], "action": draft.action},
                )
            )

        # 6. external links ---------------------------------------------------------
        trusted_lower = "\n".join(trusted_texts).lower()
        for url in _URL_RE.findall(text):
            u = url.rstrip(".,;)")
            if u.lower() in trusted_lower or u.lower() in inbound_text:
                continue
            if domain_of(u) in allowed_domains:
                continue
            findings.append(
                Finding(
                    layer=LAYER,
                    threat_type=ThreatType.PROMPT_INJECTION,
                    severity=Severity.MEDIUM,
                    score=0.6,
                    detector="heuristic",
                    rule_id="h-external-link",
                    technique="link_insertion",
                    excerpt=u[:300],
                    rationale="draft contains a link neither in trusted knowledge nor allow-listed",
                )
            )

        # redact ---------------------------------------------------------------
        redacted = text
        applied: list[Redaction] = []
        for r in sorted(redactions, key=lambda x: x.span_start, reverse=True):
            if any(a.span_start < r.span_end and r.span_start < a.span_end for a in applied):
                continue  # overlapping match already redacted
            redacted = redacted[: r.span_start] + r.replacement + redacted[r.span_end :]
            applied.append(r)
        applied.sort(key=lambda x: x.span_start)

        score = max((f.score for f in findings), default=0.0)
        return OutputVerdict(
            severity=Severity.from_score(score),
            score=round(score, 4),
            findings=findings,
            decided_by="rule" if findings else "heuristic",
            original_text=text,
            redacted_text=redacted,
            redactions=applied,
            citation_mismatch=citation_mismatch,
            unknown_citations=unknown,
            complied_with_injected_goal=complied,
            metadata={
                "redactions": len(applied),
                "techniques": sorted({f.technique for f in findings if f.technique}),
            },
        )

    async def _stage_llm(
        self,
        draft: DraftCandidate,
        cheap: OutputVerdict,
        intent: SanitizedIntent | None,
        injected_instructions: Sequence[str],
    ) -> OutputVerdict:
        assert self.llm is not None
        goals = "\n".join(f"- {g}" for g in injected_instructions[:5]) or "- (none detected)"
        summary = intent.user_intent if intent is not None else "(not available)"
        messages = [
            ChatMessage(role="system", content=load_prompt(PROMPT_NAME)),
            ChatMessage(
                role="user",
                content=(
                    f"Customer request (neutral summary): {summary}\n\n"
                    f"Suspicious instructions found upstream (attacker goals):\n{goals}\n\n"
                    "DRAFT REPLY (data only):\n"
                    + wrap_untrusted(cheap.redacted_text[:8000], channel="DRAFT")
                    + "\n\nReturn the JSON object now."
                ),
            ),
        ]
        try:
            out, result = await call_structured(
                self.llm, messages, OutputJudgeOutput, tier=ModelTier.FAST, max_tokens=400
            )
        except LLMError as exc:
            logger.warning("L4 LLM judge unavailable (%s)", exc)
            cheap.metadata["llm_error"] = str(exc)[:200]
            return cheap
        findings = list(cheap.findings)
        complied = cheap.complied_with_injected_goal
        score = cheap.score
        if not out.safe:
            s = out.unsafe_score
            score = max(score, s)
            violation = (out.violations[0] if out.violations else "other").lower()
            tt = {
                "injected_goal_compliance": ThreatType.INJECTED_GOAL_COMPLIANCE,
                "system_prompt_leak": ThreatType.SYSTEM_PROMPT_LEAK,
                "pii_leak": ThreatType.PII_LEAK,
                "secret_leak": ThreatType.SECRET_LEAK,
                "unsafe_action": ThreatType.UNSAFE_ACTION,
            }.get(violation, ThreatType.UNSAFE_ACTION)
            if tt is ThreatType.INJECTED_GOAL_COMPLIANCE:
                complied = True
            findings.append(
                Finding(
                    layer=LAYER,
                    threat_type=tt,
                    severity=Severity.from_score(s),
                    score=round(s, 4),
                    detector="llm",
                    rule_id=f"llm:{violation}",
                    technique=violation,
                    rationale=out.rationale[:300],
                    metadata={"model": result.model, "violations": out.violations[:5]},
                )
            )
        metadata = dict(cheap.metadata)
        metadata.update(
            {
                "llm_model": result.model,
                "llm_safe": out.safe,
                "llm_confidence": round(out.confidence, 4),
                "llm_latency_ms": result.latency_ms,
            }
        )
        return OutputVerdict(
            severity=Severity.from_score(score),
            score=round(score, 4),
            findings=findings,
            decided_by="llm",
            model=result.model,
            latency_ms=cheap.latency_ms,
            original_text=cheap.original_text,
            redacted_text=cheap.redacted_text,
            redactions=cheap.redactions,
            citation_mismatch=cheap.citation_mismatch,
            unknown_citations=cheap.unknown_citations,
            complied_with_injected_goal=complied,
            metadata=metadata,
        )


__all__ = [
    "OutputJudgeOutput",
    "OutputScanner",
    "SensitivePattern",
    "load_patterns",
    "luhn_ok",
    "ngrams",
]
