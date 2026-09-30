"""Layer 1 - Email Injection Scanner."""

from mailguard.layers.l1_injection_scanner.classifier import InjectionClassifier, build_pipeline
from mailguard.layers.l1_injection_scanner.llm_judge import (
    JudgeOutput,
    build_judge_messages,
    judge_email,
    wrap_untrusted,
)
from mailguard.layers.l1_injection_scanner.rules import RuleEngine, obfuscation_findings
from mailguard.layers.l1_injection_scanner.scanner import (
    EmailInjectionScanner,
    extract_indicators,
    noisy_or,
)

__all__ = [
    "EmailInjectionScanner",
    "InjectionClassifier",
    "JudgeOutput",
    "RuleEngine",
    "build_judge_messages",
    "build_pipeline",
    "extract_indicators",
    "judge_email",
    "noisy_or",
    "obfuscation_findings",
    "wrap_untrusted",
]
