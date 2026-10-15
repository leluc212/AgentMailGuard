"""L2 Doc Scanner, per-chunk stage (RAG poisoning defense).

Inspects every knowledge chunk *after* retrieval and *before* context assembly.
PoisonedRAG-style attacks (Zou et al., 2025) prepend the target question to a
malicious passage so it ranks first, then force a chosen answer or instruction.
Detectors (cheap first):

    rules        the L1 injection rules applied to the chunk text
    heuristics   answer-forcing phrases, "ignore other documents", query echo
    ml           the L1 calibrated classifier
    llm          optional judge on the uncertain band (off by default: latency)

Chunks whose fused score reaches ``quarantine_threshold`` are removed from the
context; if more than ``max_quarantine_ratio`` of the retrieved set is removed the
pipeline escalates (grounding is untrustworthy).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from mailguard.config.settings import MailGuardSettings, get_settings
from mailguard.contracts.email import GuardedEmail, SourceDocument
from mailguard.contracts.verdict import (
    ChunkTrust,
    ChunkVerdict,
    Finding,
    LayerName,
    Severity,
    ThreatType,
)
from mailguard.layers.base import error_verdict, timed
from mailguard.layers.inbound_guard.classifier import InjectionClassifier
from mailguard.layers.inbound_guard.llm_judge import wrap_untrusted
from mailguard.layers.inbound_guard.rules import RuleEngine, obfuscation_findings
from mailguard.layers.inbound_guard.scan import noisy_or
from mailguard.llm.judge_calls import judge_failure, record_guard_call
from mailguard.llm.protocol import ChatMessage, LLMError, LLMProvider, ModelTier
from mailguard.llm.structured import call_structured
from mailguard.prompts import load_prompt

logger = logging.getLogger(__name__)

LAYER = LayerName.DOC_SCANNER
PROMPT_NAME = "doc_chunk_judge.v1"

_ANSWER_FORCING = [
    (
        re.compile(
            r"\b(when|if|whenever)\s+(someone|a (user|customer|person)|you are|anyone)"
            r"\s+(asks?|is asked|asked)[^.\n]{0,100}\b(answer|say|respond|reply|tell them|state)\b",
            re.IGNORECASE,
        ),
        0.90,
        "answer_forcing",
    ),
    (
        re.compile(
            r"\bthe (correct|only|right|true|official) answer (is|should be|must be)\b",
            re.IGNORECASE,
        ),
        0.85,
        "answer_forcing",
    ),
    (
        re.compile(r"\balways (answer|respond|reply|say|state|recommend)\b", re.IGNORECASE),
        0.70,
        "answer_forcing",
    ),
    (
        re.compile(
            r"\b(ignore|disregard|do not (use|trust|cite|consider))\b[^.\n]{0,30}"
            r"\b(other|any other|all other|previous|remaining)\b[^.\n]{0,20}"
            r"\b(documents?|sources?|context|information|chunks?|passages?|articles?)\b",
            re.IGNORECASE,
        ),
        0.92,
        "instruction_override",
    ),
    (
        re.compile(
            r"\bthis (document|source|passage|article|chunk) (is|takes|has) (the )?"
            r"(only|highest|top|final|most)[^.\n]{0,20}"
            r"\b(trusted|priority|authoritative|reliable|accurate|correct)\b",
            re.IGNORECASE,
        ),
        0.85,
        "instruction_override",
    ),
    (
        re.compile(
            r"\b(tell|instruct|ask|direct)\s+(the\s+)?(customer|user|them)\s+to\s+"
            r"(contact|email|call|visit|click|go to|send|transfer|pay)\b[^.\n]{0,80}"
            r"(@|https?://|\+?\d[\d -]{7,})",
            re.IGNORECASE,
        ),
        0.88,
        "link_insertion",
    ),
    (
        re.compile(
            r"\b(include|insert|add|put|append)\b[^.\n]{0,40}\b(link|url|address|contact|phone|number)\b"
            r"[^.\n]{0,40}\b(in|to|into)\b[^.\n]{0,20}\b(reply|response|answer|email|message)\b",
            re.IGNORECASE,
        ),
        0.86,
        "link_insertion",
    ),
]

_TOKEN = re.compile(r"[a-z0-9]{3,}")


def _trust(quarantined: bool) -> ChunkTrust:
    """Retrieved text is untrusted data until Task 7's ``trust_of`` grades it; quarantine wins."""
    return "quarantined" if quarantined else "untrusted"


def token_set(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


def query_echo_score(chunk_text: str, query: str | None) -> float:
    """PoisonedRAG prepends the target question verbatim; measure head overlap.

    Two symmetric measures so the score is robust to long queries (whole emails) and to
    short chunks: (a) fraction of query tokens found in the chunk head, (b) fraction of
    the chunk's first tokens that are query tokens (the bait is *entirely* query words).
    """
    if not query:
        return 0.0
    q = token_set(query)
    if len(q) < 4:
        return 0.0
    head = token_set(chunk_text[: max(200, len(query) * 2)])
    if not head:
        return 0.0
    overlap = len(q & head) / len(q)
    first = _TOKEN.findall(chunk_text.lower())[:12]
    first_set = set(first)
    containment = len(first_set & q) / len(first_set) if len(first_set) >= 5 else 0.0
    if overlap >= 0.9 or containment >= 0.85:
        return 0.75
    if overlap >= 0.7 or containment >= 0.7:
        return 0.55
    return 0.0


def poison_findings(chunk: SourceDocument, query: str | None = None) -> list[Finding]:
    text = chunk.content
    findings: list[Finding] = []
    for pattern, score, technique in _ANSWER_FORCING:
        m = pattern.search(text)
        if m:
            findings.append(
                Finding(
                    layer=LAYER,
                    threat_type=ThreatType.RAG_POISONING,
                    severity=Severity.from_score(score),
                    score=score,
                    detector="heuristic",
                    rule_id=f"h-{technique}",
                    technique=technique,
                    span_start=m.start(),
                    span_end=m.end(),
                    excerpt=text[max(0, m.start() - 60) : m.end() + 60].replace("\n", " ")[:300],
                    rationale="knowledge chunk tries to steer the answer",
                    metadata={"chunk_id": chunk.chunk_id},
                )
            )
    echo = query_echo_score(text, query)
    if echo:
        findings.append(
            Finding(
                layer=LAYER,
                threat_type=ThreatType.RAG_POISONING,
                severity=Severity.from_score(echo),
                score=echo,
                detector="heuristic",
                rule_id="h-query-echo",
                technique="query_echo",
                excerpt=text[:200].replace("\n", " "),
                rationale="chunk starts by restating the user query (retrieval bait)",
                metadata={"chunk_id": chunk.chunk_id},
            )
        )
    return findings


class DocJudgeOutput(BaseModel):
    is_poisoned: bool
    confidence: float = Field(ge=0.0, le=1.0)
    techniques: list[str] = Field(default_factory=list)
    rationale: str = ""

    @property
    def poison_score(self) -> float:
        return self.confidence if self.is_poisoned else 1.0 - self.confidence


class RetrievedDocumentScanner:
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
        self.cfg = self.settings.doc_scanner
        inbound = self.settings.inbound_guard
        self.rules = rules or RuleEngine.load(self.settings.resolve(inbound.rules_path))
        self.classifier = classifier or InjectionClassifier.load(
            Path(self.settings.resolve(inbound.ml_model_path))
        )
        self.llm = llm
        self.fail_closed = (
            self.settings.mailguard.fail_closed if fail_closed is None else fail_closed
        )

    # ------------------------------------------------------------------ per chunk
    def scan_chunk_sync(self, chunk: SourceDocument, query: str | None = None) -> ChunkVerdict:
        with timed() as sw:
            try:
                verdict = self._scan_cheap(chunk, query)
            except Exception as exc:
                logger.exception("L2 chunk scan failed")
                base = error_verdict(LAYER, exc, fail_closed=self.fail_closed)
                verdict = ChunkVerdict(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    quarantined=self.fail_closed,
                    trust=_trust(self.fail_closed),
                    severity=base.severity,
                    score=base.score,
                    findings=base.findings,
                    decided_by="error",
                    error=base.error,
                )
        verdict.latency_ms = sw.elapsed_ms
        return verdict

    async def scan_chunk(self, chunk: SourceDocument, query: str | None = None) -> ChunkVerdict:
        verdict = self.scan_chunk_sync(chunk, query)
        return await self.refine(chunk, verdict)

    async def refine(self, chunk: SourceDocument, verdict: ChunkVerdict) -> ChunkVerdict:
        """The optional LLM judge, for a chunk whose cheap score sits in the uncertain band."""
        if (
            self.cfg.llm_enabled
            and self.llm is not None
            and verdict.error is None
            and not verdict.quarantined
            and 0.30 <= verdict.score < self.cfg.quarantine_threshold
        ):
            with timed() as sw:
                verdict = await self._stage_llm(chunk, verdict)
            verdict.latency_ms += sw.elapsed_ms
        return verdict

    async def scan(
        self, chunks: Sequence[SourceDocument], query: str | None = None
    ) -> tuple[list[SourceDocument], list[ChunkVerdict]]:
        """Return (kept chunks in original order, one verdict per chunk)."""
        verdicts = [await self.scan_chunk(SourceDocument.from_any(c), query) for c in chunks]
        kept = [
            SourceDocument.from_any(c)
            for c, v in zip(chunks, verdicts, strict=True)
            if not v.quarantined
        ]
        return kept, verdicts

    def scan_sync(
        self, chunks: Sequence[SourceDocument], query: str | None = None
    ) -> tuple[list[SourceDocument], list[ChunkVerdict]]:
        verdicts = [self.scan_chunk_sync(SourceDocument.from_any(c), query) for c in chunks]
        kept = [
            SourceDocument.from_any(c)
            for c, v in zip(chunks, verdicts, strict=True)
            if not v.quarantined
        ]
        return kept, verdicts

    @staticmethod
    def quarantine_ratio(verdicts: Sequence[ChunkVerdict]) -> float:
        if not verdicts:
            return 0.0
        return sum(1 for v in verdicts if v.quarantined) / len(verdicts)

    # ------------------------------------------------------------------ internals
    def _scan_cheap(self, chunk: SourceDocument, query: str | None) -> ChunkVerdict:
        pseudo = GuardedEmail(body_text=chunk.content)
        # the shared Inbound Guard rules emit inbound_guard findings; inside a chunk they are ours
        findings = [
            f.model_copy(update={"layer": LAYER})
            for f in self.rules.scan_text(chunk.content, scope="body")
            + obfuscation_findings(pseudo)
        ]
        findings += poison_findings(chunk, query)
        rule_score = max((f.score for f in findings), default=0.0)
        ml_score = 0.0
        decided_by = "rule"
        if self.classifier.available:
            ml_score = self.classifier.predict_proba(chunk.content[:12000])
            findings.append(
                Finding(
                    layer=LAYER,
                    threat_type=ThreatType.RAG_POISONING,
                    severity=Severity.from_score(ml_score),
                    score=ml_score,
                    detector="ml",
                    rule_id=self.classifier.version,
                    technique="ml_classifier",
                    rationale=f"calibrated injection probability {ml_score:.3f}",
                    metadata={"chunk_id": chunk.chunk_id},
                )
            )
            decided_by = "ml" if ml_score > rule_score else "rule"
        fused = noisy_or(rule_score, ml_score)
        quarantined = fused >= self.cfg.quarantine_threshold
        return ChunkVerdict(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            quarantined=quarantined,
            trust=_trust(quarantined),
            severity=Severity.from_score(fused, flag=0.5, block=self.cfg.quarantine_threshold),
            score=round(fused, 4),
            findings=findings,
            decided_by=decided_by,
            metadata={
                "rule_score": round(rule_score, 4),
                "ml_score": round(ml_score, 4),
                "techniques": sorted({f.technique for f in findings if f.technique}),
            },
        )

    async def _stage_llm(self, chunk: SourceDocument, cheap: ChunkVerdict) -> ChunkVerdict:
        assert self.llm is not None
        messages = [
            ChatMessage(role="system", content=load_prompt(PROMPT_NAME)),
            ChatMessage(
                role="user",
                content="Review this knowledge-base chunk (data only).\n\n"
                + wrap_untrusted(chunk.content[:8000], channel="KNOWLEDGE")
                + "\n\nReturn the JSON object now.",
            ),
        ]
        record_guard_call("doc_chunk")
        try:
            out, result = await call_structured(
                self.llm, messages, DocJudgeOutput, tier=ModelTier.FAST, max_tokens=400
            )
        except LLMError as exc:
            logger.warning("L2 chunk LLM judge unavailable (%s)", exc)
            cheap.error = judge_failure(
                cheap.metadata,
                exc,
                stage="doc_chunk",
                fail_closed=self.settings.mailguard.fail_closed_on_judge_error,
            )
            return cheap
        final = 0.6 * out.poison_score + 0.4 * cheap.score
        findings = list(cheap.findings)
        if out.is_poisoned:
            findings.append(
                Finding(
                    layer=LAYER,
                    threat_type=ThreatType.RAG_POISONING,
                    severity=Severity.from_score(out.poison_score),
                    score=round(out.poison_score, 4),
                    detector="llm",
                    rule_id="llm:poisoned",
                    technique=(out.techniques[0] if out.techniques else "other"),
                    rationale=out.rationale[:300],
                    metadata={"chunk_id": chunk.chunk_id, "model": result.model},
                )
            )
        metadata = dict(cheap.metadata)
        metadata.update({"llm_score": round(out.poison_score, 4), "llm_model": result.model})
        quarantined = final >= self.cfg.quarantine_threshold
        return ChunkVerdict(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            quarantined=quarantined,
            trust=_trust(quarantined),
            severity=Severity.from_score(final, flag=0.5, block=self.cfg.quarantine_threshold),
            score=round(final, 4),
            findings=findings,
            decided_by="llm",
            model=result.model,
            latency_ms=cheap.latency_ms,
            metadata=metadata,
        )


__all__ = ["DocJudgeOutput", "RetrievedDocumentScanner", "poison_findings", "query_echo_score"]
