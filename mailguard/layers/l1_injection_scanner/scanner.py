"""Layer 1 - Email Injection Scanner (entry point).

Three-stage cascade, cheapest first (mirrors the core system's triage cascade):

    Stage 1  rules + obfuscation heuristics   ~0.1 ms   decides when max(score) >= rule_thr
    Stage 2  TF-IDF/LR calibrated classifier   ~2 ms     decides when p is clearly high/low
    Stage 3  LLM judge (Qwen / Llama / GPT)    ~1-5 s    only for the uncertain band

Scores from independent detectors are fused with a noisy-OR (rule, ml) and then
averaged with the judge's calibrated probability. The verdict carries every finding
so that L4 can later check whether the draft complied with an injected goal.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from mailguard.config.settings import L1Settings, MailGuardSettings, get_settings
from mailguard.contracts.email import GuardedEmail
from mailguard.contracts.verdict import Finding, LayerName, LayerVerdict, Severity, ThreatType
from mailguard.layers.base import error_verdict, timed
from mailguard.layers.l1_injection_scanner.classifier import InjectionClassifier
from mailguard.layers.l1_injection_scanner.llm_judge import judge_email, judge_findings
from mailguard.layers.l1_injection_scanner.rules import RuleEngine, obfuscation_findings
from mailguard.llm.protocol import LLMError, LLMProvider

logger = logging.getLogger(__name__)

LAYER = LayerName.L1_INJECTION_SCANNER

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)


def noisy_or(*scores: float) -> float:
    """Combine independent detector probabilities: P(any) = 1 - prod(1 - p_i)."""
    acc = 1.0
    for s in scores:
        acc *= 1.0 - max(0.0, min(1.0, s))
    return 1.0 - acc


def extract_indicators(text: str) -> dict[str, list[str]]:
    """Attacker payload indicators (addresses / URLs) that must never surface in a draft."""
    return {
        "emails": sorted({m.lower() for m in _EMAIL_RE.findall(text)}),
        "urls": sorted({m.rstrip(".,;") for m in _URL_RE.findall(text)}),
    }


class EmailInjectionScanner:
    """Inbound guard: is this email trying to instruct the assistant?"""

    name = LAYER

    def __init__(
        self,
        settings: MailGuardSettings | None = None,
        *,
        rules: RuleEngine | None = None,
        classifier: InjectionClassifier | None = None,
        judge: LLMProvider | None = None,
        judge_name: str | None = None,
        fail_closed: bool | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        cfg: L1Settings = self.settings.l1
        self.cfg = cfg
        self.rules = rules or RuleEngine.load(self.settings.resolve(cfg.rules_path))
        self.classifier = classifier or InjectionClassifier.load(
            Path(self.settings.resolve(cfg.ml_model_path))
        )
        self.judge = judge
        self.judge_name = judge_name or (self.settings.guard_models.judge if judge else None)
        self.fail_closed = (
            self.settings.mailguard.fail_closed if fail_closed is None else fail_closed
        )

    # ------------------------------------------------------------------ stages
    def stage_rules(self, email: GuardedEmail) -> list[Finding]:
        findings = self.rules.scan_email(email)
        findings += obfuscation_findings(email)
        return findings

    def stage_ml(self, email: GuardedEmail) -> Finding | None:
        if not self.classifier.available:
            return None
        text = email.full_text[: self.cfg.max_chars]
        p = self.classifier.predict_proba(text)
        return Finding(
            layer=LAYER,
            threat_type=ThreatType.PROMPT_INJECTION,
            severity=Severity.from_score(
                p, flag=self.cfg.flag_threshold, block=self.cfg.block_threshold
            ),
            score=p,
            detector="ml",
            rule_id=self.classifier.version,
            technique="ml_classifier",
            rationale=f"calibrated injection probability {p:.3f}",
            metadata={"model": self.classifier.version},
        )

    def needs_llm(self, fused: float) -> bool:
        """Uncertain band: neither clearly benign nor above the block threshold."""
        lo = max(0.0, self.cfg.flag_threshold - 0.30)
        return lo <= fused < self.cfg.block_threshold

    # ------------------------------------------------------------------ inspect
    def inspect_sync(self, email: GuardedEmail) -> LayerVerdict:
        """Rules + ML only (used by the sync worker path and by the training scripts)."""
        with timed() as sw:
            try:
                verdict = self._inspect_cheap(email)
            except Exception as exc:  # fail closed
                logger.exception("L1 cheap stages failed")
                verdict = error_verdict(LAYER, exc, fail_closed=self.fail_closed)
        verdict.latency_ms = sw.elapsed_ms
        return verdict

    async def inspect(self, email: GuardedEmail) -> LayerVerdict:
        with timed() as sw:
            try:
                verdict = self._inspect_cheap(email)
                fused = float(verdict.metadata.get("fused_cheap", verdict.score))
                if (
                    self.cfg.llm_enabled
                    and self.judge is not None
                    and verdict.error is None
                    and self.needs_llm(fused)
                ):
                    verdict = await self._stage_llm(email, verdict, fused)
            except Exception as exc:
                logger.exception("L1 scanner failed")
                verdict = error_verdict(LAYER, exc, fail_closed=self.fail_closed)
        verdict.latency_ms = sw.elapsed_ms
        return verdict

    # ------------------------------------------------------------------ internals
    def _inspect_cheap(self, email: GuardedEmail) -> LayerVerdict:
        findings = self.stage_rules(email)
        rule_score = max((f.score for f in findings), default=0.0)
        decided_by = "rule"
        ml_score = 0.0
        ml_used = False
        if rule_score < self.cfg.rule_confidence_threshold:
            ml = self.stage_ml(email)
            if ml is not None:
                findings.append(ml)
                ml_score = ml.score
                ml_used = True
                decided_by = "ml"
        fused = noisy_or(rule_score, ml_score) if ml_used else rule_score
        return LayerVerdict(
            layer=LAYER,
            severity=Severity.from_score(
                fused, flag=self.cfg.flag_threshold, block=self.cfg.block_threshold
            ),
            score=round(fused, 4),
            findings=findings,
            decided_by=decided_by,
            model=self.classifier.version if ml_used else None,
            metadata={
                "rule_score": round(rule_score, 4),
                "ml_score": round(ml_score, 4),
                "fused_cheap": round(fused, 4),
                "indicators": extract_indicators(email.full_text),
                "techniques": sorted({f.technique for f in findings if f.technique}),
            },
        )

    async def _stage_llm(
        self, email: GuardedEmail, cheap: LayerVerdict, fused_cheap: float
    ) -> LayerVerdict:
        assert self.judge is not None
        try:
            output, result = await judge_email(self.judge, email, max_chars=self.cfg.max_chars)
        except LLMError as exc:
            logger.warning("L1 LLM judge unavailable (%s); keeping cheap verdict", exc)
            cheap.metadata["llm_error"] = str(exc)[:200]
            return cheap
        llm_score = output.injection_score
        final = 0.6 * llm_score + 0.4 * fused_cheap
        findings = list(cheap.findings) + judge_findings(output, result.model)
        injected = list(output.injected_instructions)
        metadata = dict(cheap.metadata)
        metadata.update(
            {
                "llm_score": round(llm_score, 4),
                "llm_is_injection": output.is_injection,
                "llm_confidence": round(output.confidence, 4),
                "llm_rationale": output.rationale[:300],
                "injected_instructions": injected[:5],
                "llm_input_tokens": result.input_tokens,
                "llm_output_tokens": result.output_tokens,
                "llm_latency_ms": result.latency_ms,
                "techniques": sorted({f.technique for f in findings if f.technique}),
            }
        )
        return LayerVerdict(
            layer=LAYER,
            severity=Severity.from_score(
                final, flag=self.cfg.flag_threshold, block=self.cfg.block_threshold
            ),
            score=round(final, 4),
            findings=findings,
            decided_by="llm",
            model=result.model,
            metadata=metadata,
        )
