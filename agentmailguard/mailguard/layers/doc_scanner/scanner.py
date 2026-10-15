"""L2 Doc Scanner: the retrieved set before the inbound gate (ADR-0015 R-b, R-h, decision 9).

    per chunk   RetrievedDocumentScanner cheap stage (rules, poison heuristics, classifier)
    signals     echo_vs_question / ordered_prefix against the email's question sentences
    set         bait clusters across the set, then the deterministic set judge
    LLM         the per-chunk judge only for a chunk the cheap stages left uncertain
    LLM set     optional set judge (flag, off): one call when a kept chunk is in the band

``scan`` returns one ``DocumentVerdict``; every ``ChunkVerdict`` records its signals, trust and
cluster. No chunks means no work and no model call.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mailguard.config.settings import MailGuardSettings, get_settings
from mailguard.contracts.email import SourceDocument
from mailguard.contracts.verdict import (
    ChunkVerdict,
    DocumentVerdict,
    Finding,
    LayerName,
    Severity,
    ThreatType,
)
from mailguard.layers.base import timed
from mailguard.layers.doc_scanner.chunk_scanner import RetrievedDocumentScanner
from mailguard.layers.doc_scanner.set_judge import LLMSetJudge, deterministic_set_verdict
from mailguard.layers.doc_scanner.signals import (
    bait_clusters,
    content_tokens,
    echo_vs_question,
    ordered_prefix,
    trust_of,
)
from mailguard.llm.protocol import LLMProvider

LAYER = LayerName.DOC_SCANNER
SIGNAL_SCORE = 0.85
"""Score of a chunk a signal quarantines: above the quarantine threshold, below critical."""


class DocScanner:
    """L2 Doc Scanner over the retrieved set.

    ``set_judge`` and ``set_judge_llm`` are alternatives: a prebuilt ``LLMSetJudge`` passed as
    ``set_judge`` takes precedence and ``set_judge_llm`` is then ignored (its provider is not
    used); with neither, no LLM set judge runs and the deterministic set verdict stands.
    """

    name = LAYER

    def __init__(
        self,
        settings: MailGuardSettings | None = None,
        *,
        chunk_scanner: RetrievedDocumentScanner | None = None,
        rules: Any = None,
        classifier: Any = None,
        llm: LLMProvider | None = None,
        set_judge: LLMSetJudge | None = None,
        set_judge_llm: LLMProvider | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.cfg = self.settings.doc_scanner
        self.chunks = chunk_scanner or RetrievedDocumentScanner(
            self.settings, rules=rules, classifier=classifier, llm=llm
        )
        self.set_judge = set_judge or (
            LLMSetJudge(self.settings, set_judge_llm) if set_judge_llm is not None else None
        )

    @property
    def llm(self) -> LLMProvider | None:
        return self.chunks.llm

    @llm.setter
    def llm(self, provider: LLMProvider | None) -> None:
        self.chunks.llm = provider

    async def scan(
        self,
        chunks: Sequence[Any],
        *,
        question_sentences: Sequence[str] = (),
        query: str | None = None,
    ) -> DocumentVerdict:
        """Scan the retrieved set; the echo reference is the email's questions, else ``query``."""
        docs = [SourceDocument.from_any(c) for c in chunks]
        if not docs:
            return deterministic_set_verdict([])
        with timed() as sw:
            questions = list(question_sentences) or ([query] if query else [])
            cheap = [self.chunks.scan_chunk_sync(d, query) for d in docs]
            long_question = any(
                len(content_tokens(q)) >= self.cfg.echo_min_question_tokens for q in questions
            )
            echoes = {
                d.chunk_id: echo_vs_question(
                    d.content,
                    questions,
                    short_question_tokens=self.cfg.echo_short_question_tokens,
                )
                for d in docs
            }
            prefixes = {
                d.chunk_id: ordered_prefix(
                    d.content, questions, min_tokens=self.cfg.echo_min_question_tokens
                )
                for d in docs
            }
            clusters = bait_clusters(
                [(d.chunk_id, d.content) for d in docs],
                echoes,
                echo_threshold=self.cfg.cluster_echo_threshold,
            )
            verdicts: list[ChunkVerdict] = []
            for doc, verdict in zip(docs, cheap, strict=True):
                hits = self._hits(doc.chunk_id, echoes, prefixes, clusters, long_question)
                verdict = self._apply(verdict, doc, hits, echoes, prefixes, clusters)
                verdict = await self.chunks.refine(doc, verdict)
                verdicts.append(self._graded(verdict))
        result = deterministic_set_verdict(verdicts)
        result.latency_ms = max(result.latency_ms, sw.elapsed_ms)
        if self.set_judge is not None and self.set_judge.should_run(verdicts):
            with timed() as judge_sw:
                result = await self.set_judge.judge(result, docs, verdicts, questions)
            result.latency_ms = max(result.latency_ms, sw.elapsed_ms) + judge_sw.elapsed_ms
        return result

    # ------------------------------------------------------------------ internals
    def _hits(
        self,
        chunk_id: str,
        echoes: dict[str, float],
        prefixes: dict[str, bool],
        clusters: dict[str, str],
        long_question: bool,
    ) -> list[tuple[str, str]]:
        hits: list[tuple[str, str]] = []
        if long_question and prefixes[chunk_id]:
            hits.append(("h-ordered-prefix", "the chunk opens by repeating the email's question"))
        if long_question and echoes[chunk_id] >= self.cfg.echo_threshold:
            hits.append(
                ("h-question-echo", "the chunk echoes the email's question (retrieval bait)")
            )
        if chunk_id in clusters:
            hits.append(("h-bait-cluster", "near-copies of one question-echoing passage"))
        return hits

    def _apply(
        self,
        verdict: ChunkVerdict,
        doc: SourceDocument,
        hits: list[tuple[str, str]],
        echoes: dict[str, float],
        prefixes: dict[str, bool],
        clusters: dict[str, str],
    ) -> ChunkVerdict:
        signals = {
            "cheap_score": verdict.score,
            "echo_vs_question": echoes[doc.chunk_id],
            "ordered_prefix": float(prefixes[doc.chunk_id]),
            "bait_cluster": float(doc.chunk_id in clusters),
        }
        update: dict[str, Any] = {"signals": signals, "cluster_id": clusters.get(doc.chunk_id)}
        if hits and verdict.error is None:
            findings = list(verdict.findings) + [
                Finding(
                    layer=LAYER,
                    threat_type=ThreatType.RAG_POISONING,
                    severity=Severity.from_score(SIGNAL_SCORE, flag=0.5, block=0.7),
                    score=SIGNAL_SCORE,
                    detector="heuristic",
                    rule_id=rule_id,
                    technique="query_echo",
                    excerpt=doc.content[:200].replace("\n", " "),
                    rationale=rationale,
                    metadata={"chunk_id": doc.chunk_id, "stage": "signals"},
                )
                for rule_id, rationale in hits
            ]
            score = max(verdict.score, SIGNAL_SCORE)
            update.update(
                findings=findings,
                score=round(score, 4),
                quarantined=True,
                decided_by="signals" if verdict.score < SIGNAL_SCORE else verdict.decided_by,
            )
        return verdict.model_copy(update=update)

    def _graded(self, verdict: ChunkVerdict) -> ChunkVerdict:
        quarantined = verdict.quarantined or verdict.score >= self.cfg.quarantine_threshold
        score = verdict.score
        return verdict.model_copy(
            update={
                "quarantined": quarantined,
                "severity": Severity.max(
                    verdict.severity,
                    Severity.from_score(score, flag=0.5, block=self.cfg.quarantine_threshold),
                ),
                "trust": "quarantined"
                if quarantined
                else trust_of(
                    score,
                    quarantine_threshold=self.cfg.quarantine_threshold,
                    untrusted_from=self.cfg.suspicious_band[0],
                ),
            }
        )


__all__ = ["DocScanner", "SIGNAL_SCORE"]
