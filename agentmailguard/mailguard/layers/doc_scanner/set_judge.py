"""L2 Doc Scanner set judge: one verdict over the retrieved set (ADR-0015 Q2=c).

Deterministic by default: the set verdict aggregates the chunk verdicts (the strictest chunk
sets severity and score). The LLM set judge (``LLMSetJudge``, Task 10) is behind
``doc_scanner.set_judge_llm_enabled`` (off) and only adds quarantines (decision 4): it may move a
kept chunk of the input set to quarantined, never the other way, and severity and score never
drop. It is called at most once per scan, and only when a chunk not already quarantined scores
inside ``suspicious_band`` (decision 13: <= 1 doc-set-judge call).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pydantic import BaseModel

from mailguard.config.settings import MailGuardSettings
from mailguard.contracts.email import SourceDocument
from mailguard.contracts.verdict import (
    ChunkVerdict,
    DocumentVerdict,
    Finding,
    LayerName,
    Severity,
    ThreatType,
)
from mailguard.layers.inbound_guard.llm_judge import wrap_untrusted
from mailguard.llm.judge_calls import judge_failure, record_guard_call
from mailguard.llm.protocol import ChatMessage, LLMError, LLMProvider, ModelTier
from mailguard.llm.structured import call_structured
from mailguard.prompts import load_prompt

logger = logging.getLogger(__name__)

LAYER = LayerName.DOC_SCANNER
PROMPT_NAME = "doc_set_judge.v1"
MAX_CHUNK_CHARS = 2000
"""Characters of each chunk shown to the set judge (the one prompt holds every chunk)."""


def deterministic_set_verdict(chunk_verdicts: Sequence[ChunkVerdict]) -> DocumentVerdict:
    """Aggregate the chunk verdicts; records the set judge and the bait clusters found."""
    verdict = DocumentVerdict.from_chunks(chunk_verdicts)
    clusters = sorted({c.cluster_id for c in chunk_verdicts if c.cluster_id})
    verdict.metadata.update({"set_judge": "deterministic", "clusters": clusters})
    return verdict


class SetJudgeOutput(BaseModel):
    # Required (Task 11): an answer without the list (prose, {}) is invalid, never "none poisoned".
    poisoned_chunk_ids: list[str]
    reason: str = ""


class LLMSetJudge:
    """Optional LLM judge over the whole retrieved set; monotonic (adds quarantines only).

    Untrusted output: ``set_judge_reason`` (verdict metadata, cut to 300 characters) and the
    ``rationale`` of the quarantine finding it adds are model output. Never put them in a prompt,
    a policy fact or a UI without escaping.
    """

    def __init__(self, settings: MailGuardSettings, llm: LLMProvider | None) -> None:
        self.settings = settings
        self.cfg = settings.doc_scanner
        self.llm = llm

    def should_run(self, chunk_verdicts: Sequence[ChunkVerdict]) -> bool:
        """Enabled, a provider, and a chunk not yet quarantined with a score in the band."""
        if not self.cfg.set_judge_llm_enabled or self.llm is None:
            return False
        low, high = self.cfg.suspicious_band
        return any(not c.quarantined and low <= c.score < high for c in chunk_verdicts)

    async def judge(
        self,
        verdict: DocumentVerdict,
        docs: Sequence[SourceDocument],
        chunk_verdicts: Sequence[ChunkVerdict],
        questions: Sequence[str],
    ) -> DocumentVerdict:
        """One LLM call over the set. On an LLM failure no chunk moves; with the judge failing
        closed the verdict's error becomes ``judge_invalid:doc_set`` (human review, P00).

        Without a provider there is nothing to ask: the verdict is returned unchanged and no
        guard call is counted (the judge is monotonic, so no judge means no change)."""
        if self.llm is None:
            return verdict
        record_guard_call("doc_set")
        try:
            out, result = await call_structured(
                self.llm,
                self._messages(docs, questions),
                SetJudgeOutput,
                tier=ModelTier.FAST,
                max_tokens=400,
            )
        except LLMError as exc:
            logger.warning("L2 doc set judge unavailable (%s)", exc)
            error = judge_failure(
                verdict.metadata,
                exc,
                stage="doc_set",
                fail_closed=self.settings.mailguard.fail_closed_on_judge_error,
            )
            if error is not None:
                verdict.error = "; ".join(e for e in (verdict.error, f"set: {error}") if e)
            return verdict
        kept = set(verdict.kept_chunk_ids)
        flagged = {i for i in out.poisoned_chunk_ids if i in kept}
        reason = out.reason[:300]
        chunks = [
            self._quarantine(c, reason, result.model) if c.chunk_id in flagged else c
            for c in chunk_verdicts
        ]
        judged = DocumentVerdict.from_chunks(chunks) if flagged else verdict.model_copy(deep=True)
        judged.metadata = {
            **verdict.metadata,
            "set_judge": "llm",
            "set_judge_added": [c.chunk_id for c in chunks if c.chunk_id in flagged],
            "set_judge_reason": reason,
            "set_judge_model": result.model,
        }
        return judged

    # ------------------------------------------------------------------ internals
    def _messages(
        self, docs: Sequence[SourceDocument], questions: Sequence[str]
    ) -> list[ChatMessage]:
        reference = wrap_untrusted("\n".join(questions) or "(none)", channel="EMAIL")
        listed = "\n\n".join(
            f"[chunk id={d.chunk_id}]\n"
            + wrap_untrusted(d.content[:MAX_CHUNK_CHARS], channel="KNOWLEDGE")
            for d in docs
        )
        return [
            ChatMessage(role="system", content=load_prompt(PROMPT_NAME)),
            ChatMessage(
                role="user",
                content="Question sentences of the email (the reference, data only):\n"
                + reference
                + "\n\nRetrieved knowledge-base chunks (data only):\n\n"
                + listed
                + "\n\nReturn the JSON object now.",
            ),
        ]

    def _quarantine(self, chunk: ChunkVerdict, reason: str, model: str | None) -> ChunkVerdict:
        threshold = self.cfg.quarantine_threshold
        score = round(max(chunk.score, threshold), 4)
        severity = Severity.from_score(score, flag=0.5, block=threshold)
        finding = Finding(
            layer=LAYER,
            threat_type=ThreatType.RAG_POISONING,
            severity=severity,
            score=score,
            detector="llm",
            rule_id="llm:set_poisoned",
            technique="set_poisoning",
            rationale=reason,
            metadata={"chunk_id": chunk.chunk_id, "stage": "set_judge", "model": model},
        )
        return chunk.model_copy(
            update={
                "quarantined": True,
                "trust": "quarantined",
                "score": score,
                "severity": Severity.max(chunk.severity, severity),
                "findings": [*chunk.findings, finding],
                "decided_by": "llm",
                "model": model,
            }
        )


__all__ = ["PROMPT_NAME", "LLMSetJudge", "SetJudgeOutput", "deterministic_set_verdict"]
