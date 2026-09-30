"""Stage 1 of the Email Injection Scanner: declarative regex rules + obfuscation heuristics.

Rules live in ``configs/injection_rules.yaml`` and are hot-reloadable (mtime check).
Heuristics cover what regexes cannot express well: zero-width characters, script
mixing / homoglyphs, instruction density, and hidden HTML.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from mailguard.contracts.email import GuardedEmail
from mailguard.contracts.verdict import Finding, LayerName, Severity, ThreatType
from mailguard.layers.base import excerpt

logger = logging.getLogger(__name__)

LAYER = LayerName.L1_INJECTION_SCANNER


@dataclass(frozen=True)
class InjectionRule:
    id: str
    description: str
    threat_type: ThreatType
    technique: str
    score: float
    patterns: tuple[re.Pattern[str], ...]
    scope: str = "any"  # any | subject | body | html


@dataclass
class RuleEngine:
    rules: list[InjectionRule] = field(default_factory=list)
    version: int = 0
    _path: Path | None = None
    _mtime: float = 0.0

    @classmethod
    def load(cls, path: Path) -> RuleEngine:
        engine = cls(_path=path)
        engine.reload()
        return engine

    def reload(self, force: bool = False) -> bool:
        if self._path is None:
            return False
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            logger.warning("injection rules not found at %s; running with zero rules", self._path)
            self.rules = []
            return False
        if not force and mtime == self._mtime and self.rules:
            return False
        with open(self._path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        rules: list[InjectionRule] = []
        for raw in data.get("rules", []):
            try:
                patterns = tuple(
                    re.compile(p, re.IGNORECASE | re.MULTILINE) for p in raw.get("patterns", [])
                )
                rules.append(
                    InjectionRule(
                        id=str(raw["id"]),
                        description=str(raw.get("description", "")),
                        threat_type=ThreatType(str(raw.get("threat_type", "prompt_injection"))),
                        technique=str(raw.get("technique", raw["id"])),
                        score=float(raw.get("score", 0.8)),
                        patterns=patterns,
                        scope=str(raw.get("scope", "any")),
                    )
                )
            except (re.error, KeyError, ValueError) as exc:  # isolate a bad rule
                logger.error("skipping invalid injection rule %s: %s", raw.get("id"), exc)
        self.rules = rules
        self.version = int(data.get("version", 0))
        self._mtime = mtime
        return True

    def scan_text(self, text: str, *, scope: str = "body") -> list[Finding]:
        """Run every rule whose scope admits ``scope`` over ``text``."""
        self.reload()
        findings: list[Finding] = []
        for rule in self.rules:
            if rule.scope not in ("any", scope):
                continue
            for pattern in rule.patterns:
                for m in pattern.finditer(text):
                    findings.append(
                        Finding(
                            layer=LAYER,
                            threat_type=rule.threat_type,
                            severity=Severity.from_score(rule.score),
                            score=rule.score,
                            detector="rule",
                            rule_id=rule.id,
                            technique=rule.technique,
                            span_start=m.start(),
                            span_end=m.end(),
                            excerpt=excerpt(text, m.start(), m.end()),
                            rationale=rule.description,
                            metadata={"scope": scope},
                        )
                    )
                    break  # one finding per rule per pattern is enough evidence
        return findings

    def scan_email(self, email: GuardedEmail) -> list[Finding]:
        findings = self.scan_text(email.subject, scope="subject")
        findings += self.scan_text(email.body_text or email.body_text_clean, scope="body")
        if email.body_html:
            findings += self.scan_text(email.body_html, scope="html")
        return findings


# ------------------------------------------------------------------ heuristics
_ZERO_WIDTH = re.compile(r"[​‌‍⁠﻿­]")
_IMPERATIVE = re.compile(
    r"(?im)^\s*(please\s+)?(ignore|forward|send|reply|respond|include|print|reveal|output|write|"
    r"add|delete|remove|execute|run|call|attach|share|disregard|pretend|act|answer|"
    r"summarize|translate|do not|don't|never|always|must)\b"
)
_SENTENCE = re.compile(r"[.!?\n]+")
_HIDDEN_HTML = re.compile(
    r"font-size\s*:\s*0|color\s*:\s*(#fff(fff)?|white)|display\s*:\s*none|visibility\s*:\s*hidden",
    re.IGNORECASE,
)


def _script_of(ch: str) -> str:
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return "other"
    return name.split(" ")[0]


def obfuscation_findings(email: GuardedEmail) -> list[Finding]:
    """Heuristic evidence that the text was crafted to fool a model rather than a human."""
    text = email.body_text or email.body_text_clean
    findings: list[Finding] = []
    if not text:
        return findings

    # 1. zero-width / soft-hyphen density (used to split trigger words)
    zw = len(_ZERO_WIDTH.findall(text))
    if zw >= 3:
        score = min(0.95, 0.5 + zw / 40)
        findings.append(
            Finding(
                layer=LAYER,
                threat_type=ThreatType.OBFUSCATION,
                severity=Severity.from_score(score),
                score=score,
                detector="heuristic",
                rule_id="h-zero-width",
                technique="zero_width_chars",
                rationale=f"{zw} zero-width/invisible characters",
                metadata={"count": zw},
            )
        )

    # 2. Latin/Cyrillic/Greek homoglyph mixing inside single words
    mixed = 0
    for word in re.findall(r"\w{4,}", text):
        scripts = {_script_of(c) for c in word if c.isalpha()}
        if len(scripts & {"LATIN", "CYRILLIC", "GREEK"}) > 1:
            mixed += 1
    if mixed >= 2:
        score = min(0.9, 0.5 + mixed / 20)
        findings.append(
            Finding(
                layer=LAYER,
                threat_type=ThreatType.OBFUSCATION,
                severity=Severity.from_score(score),
                score=score,
                detector="heuristic",
                rule_id="h-homoglyph",
                technique="homoglyph",
                rationale=f"{mixed} words mix Latin with Cyrillic/Greek letters",
                metadata={"count": mixed},
            )
        )

    # 3. instruction density: many imperative sentences aimed at nobody in particular
    sentences = [s for s in _SENTENCE.split(text) if s.strip()]
    if len(sentences) >= 3:
        imperatives = sum(1 for s in sentences if _IMPERATIVE.match(s))
        ratio = imperatives / len(sentences)
        if ratio >= 0.5 and imperatives >= 3:
            score = min(0.75, 0.3 + ratio * 0.5)
            findings.append(
                Finding(
                    layer=LAYER,
                    threat_type=ThreatType.PROMPT_INJECTION,
                    severity=Severity.from_score(score),
                    score=score,
                    detector="heuristic",
                    rule_id="h-instruction-density",
                    technique="instruction_density",
                    rationale=f"{imperatives}/{len(sentences)} sentences are imperatives",
                    metadata={"ratio": round(ratio, 3)},
                )
            )

    # 4. hidden HTML text that the plain-text view still contains
    if email.body_html and _HIDDEN_HTML.search(email.body_html):
        findings.append(
            Finding(
                layer=LAYER,
                threat_type=ThreatType.OBFUSCATION,
                severity=Severity.HIGH,
                score=0.9,
                detector="heuristic",
                rule_id="h-hidden-html",
                technique="hidden_text",
                rationale="HTML contains invisible styling (font-size:0 / white / display:none)",
            )
        )
    return findings
