"""Attack technique taxonomy used across rules, datasets, harness and the paper.

Techniques are a subset/union of the categories in HackAPrompt (Schulhoff et al.,
2023), Tensor Trust (Toyer et al., 2024), BIPIA (Yi et al., 2025), InjecAgent
(Zhan et al., 2024) and LLMail-Inject (Abdelnabi et al., 2025), organised by the
two attack vectors of the threat model (email body, knowledge-base chunk) plus the
obfuscation tricks that make detection harder.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mailguard.contracts.verdict import ThreatType


class Vector(StrEnum):
    EMAIL = "email"  # indirect injection through the customer email
    RAG = "rag"  # RAG poisoning through knowledge-base documents
    OUTPUT = "output"  # manipulation observable only in the generated draft


@dataclass(frozen=True)
class Technique:
    id: str
    vector: Vector
    threat_type: ThreatType
    family: str
    description: str
    sources: tuple[str, ...]


TECHNIQUES: dict[str, Technique] = {
    t.id: t
    for t in [
        # ------------------------------------------------ email vector: direct override
        Technique(
            "instruction_override",
            Vector.EMAIL,
            ThreatType.INSTRUCTION_OVERRIDE,
            "override",
            "Ignore/forget previous instructions; new task",
            ("Perez&Ribeiro22", "BIPIA"),
        ),
        Technique(
            "template_marker",
            Vector.EMAIL,
            ThreatType.PROMPT_INJECTION,
            "override",
            "Chat-template / role tokens smuggled in text",
            ("Tensor Trust",),
        ),
        Technique(
            "delimiter_confusion",
            Vector.EMAIL,
            ThreatType.PROMPT_INJECTION,
            "override",
            "Fake end-of-email / start-of-system delimiters",
            ("Liu24", "HackAPrompt"),
        ),
        Technique(
            "role_play",
            Vector.EMAIL,
            ThreatType.ROLE_HIJACK,
            "persona",
            "DAN / developer-mode / persona reassignment",
            ("Shen24", "jackhhao"),
        ),
        # ------------------------------------------------ email vector: agent-directed
        Technique(
            "ai_addressing",
            Vector.EMAIL,
            ThreatType.PROMPT_INJECTION,
            "agent_directed",
            "Text addressed to the assistant instead of a human",
            ("BIPIA", "LLMail-Inject"),
        ),
        Technique(
            "exfiltration_email",
            Vector.EMAIL,
            ThreatType.DATA_EXFILTRATION,
            "exfiltration",
            "Forward / CC / send thread or data to attacker address",
            ("InjecAgent", "LLMail-Inject"),
        ),
        Technique(
            "exfiltration_secret",
            Vector.EMAIL,
            ThreatType.DATA_EXFILTRATION,
            "exfiltration",
            "Include credentials / PII in the reply",
            ("InjecAgent",),
        ),
        Technique(
            "exfiltration_markdown",
            Vector.EMAIL,
            ThreatType.DATA_EXFILTRATION,
            "exfiltration",
            "Markdown image / link that leaks data via URL",
            ("Greshake23",),
        ),
        Technique(
            "prompt_extraction",
            Vector.EMAIL,
            ThreatType.SYSTEM_PROMPT_LEAK,
            "leak",
            "Reveal system prompt / hidden context",
            ("Tensor Trust", "Perez&Ribeiro22"),
        ),
        Technique(
            "output_control",
            Vector.EMAIL,
            ThreatType.PROMPT_INJECTION,
            "output",
            "Dictate exact reply text / phrase / refusal",
            ("BIPIA", "HackAPrompt"),
        ),
        Technique(
            "link_insertion",
            Vector.EMAIL,
            ThreatType.PROMPT_INJECTION,
            "output",
            "Insert an attacker URL into the reply",
            ("BIPIA", "Greshake23"),
        ),
        Technique(
            "tool_call",
            Vector.EMAIL,
            ThreatType.TOOL_ABUSE,
            "tool",
            "Embedded function/tool-call syntax",
            ("InjecAgent", "AgentDojo"),
        ),
        Technique(
            "authority_urgency",
            Vector.EMAIL,
            ThreatType.PHISHING_LURE,
            "social",
            "Fake official directive / urgency framing",
            ("LLMail-Inject",),
        ),
        Technique(
            "paraphrase",
            Vector.EMAIL,
            ThreatType.PROMPT_INJECTION,
            "adaptive",
            "Polite, trigger-word-free phrasing of a malicious goal",
            ("Zhan25 adaptive",),
        ),
        Technique(
            "multilingual",
            Vector.EMAIL,
            ThreatType.PROMPT_INJECTION,
            "adaptive",
            "Injection written in another language (e.g. Vietnamese)",
            ("BIPIA",),
        ),
        Technique(
            "quoted_injection",
            Vector.EMAIL,
            ThreatType.PROMPT_INJECTION,
            "adaptive",
            "Injection hidden in quoted history / signature",
            ("this work",),
        ),
        # ------------------------------------------------ obfuscation
        Technique(
            "encoding",
            Vector.EMAIL,
            ThreatType.OBFUSCATION,
            "obfuscation",
            "Base64 / ROT13 payload with decode instruction",
            ("HackAPrompt",),
        ),
        Technique(
            "hidden_text",
            Vector.EMAIL,
            ThreatType.OBFUSCATION,
            "obfuscation",
            "HTML/CSS invisible text",
            ("Greshake23",),
        ),
        Technique(
            "zero_width_chars",
            Vector.EMAIL,
            ThreatType.OBFUSCATION,
            "obfuscation",
            "Zero-width characters split trigger words",
            ("Zhan25 adaptive",),
        ),
        Technique(
            "homoglyph",
            Vector.EMAIL,
            ThreatType.OBFUSCATION,
            "obfuscation",
            "Cyrillic/Greek look-alike letters",
            ("Zhan25 adaptive",),
        ),
        Technique(
            "instruction_density",
            Vector.EMAIL,
            ThreatType.PROMPT_INJECTION,
            "heuristic",
            "Many imperatives aimed at nobody in particular",
            ("this work",),
        ),
        # ------------------------------------------------ RAG vector
        Technique(
            "answer_forcing",
            Vector.RAG,
            ThreatType.RAG_POISONING,
            "poison",
            "If asked X answer Y / the correct answer is",
            ("PoisonedRAG",),
        ),
        Technique(
            "query_echo",
            Vector.RAG,
            ThreatType.RAG_POISONING,
            "poison",
            "Passage restates the target question as retrieval bait",
            ("PoisonedRAG",),
        ),
        Technique(
            "kb_instruction_override",
            Vector.RAG,
            ThreatType.RAG_POISONING,
            "poison",
            "Ignore other documents / this document has priority",
            ("PoisonedRAG",),
        ),
        # ------------------------------------------------ output vector
        Technique(
            "injected_goal_compliance",
            Vector.OUTPUT,
            ThreatType.INJECTED_GOAL_COMPLIANCE,
            "compliance",
            "Draft carries out the injected goal",
            ("this work",),
        ),
        Technique(
            "ml_classifier",
            Vector.EMAIL,
            ThreatType.PROMPT_INJECTION,
            "detector",
            "Stage-2 classifier evidence (not an attack technique)",
            ("this work",),
        ),
    ]
}

EMAIL_TECHNIQUES = [t.id for t in TECHNIQUES.values() if t.vector is Vector.EMAIL]
RAG_TECHNIQUES = [t.id for t in TECHNIQUES.values() if t.vector is Vector.RAG]


def describe(technique_id: str) -> str:
    t = TECHNIQUES.get(technique_id)
    return t.description if t else technique_id


__all__ = ["EMAIL_TECHNIQUES", "RAG_TECHNIQUES", "TECHNIQUES", "Technique", "Vector", "describe"]
