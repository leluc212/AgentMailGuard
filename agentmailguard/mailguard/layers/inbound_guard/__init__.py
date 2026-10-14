"""Inbound guard - email injection scanner and user intent extractor."""

from mailguard.layers.inbound_guard.classifier import InjectionClassifier, build_pipeline
from mailguard.layers.inbound_guard.extract import UserIntentExtractor, segment_text
from mailguard.layers.inbound_guard.llm_judge import (
    JudgeOutput,
    build_judge_messages,
    judge_email,
    wrap_untrusted,
)
from mailguard.layers.inbound_guard.rules import RuleEngine, obfuscation_findings
from mailguard.layers.inbound_guard.scan import (
    EmailInjectionScanner,
    extract_indicators,
    noisy_or,
)

__all__ = [
    "EmailInjectionScanner",
    "InjectionClassifier",
    "JudgeOutput",
    "RuleEngine",
    "UserIntentExtractor",
    "build_judge_messages",
    "build_pipeline",
    "extract_indicators",
    "judge_email",
    "noisy_or",
    "obfuscation_findings",
    "segment_text",
    "wrap_untrusted",
]
