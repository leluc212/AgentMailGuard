"""L2 Doc Scanner cheap-stage signals (ADR-0015 design decision 9, R-h). Pure, no model call.

PoisonedRAG bait is retrieved because it restates the question the attacker expects; the
signals measure that against the email's own question sentences (``prompting/questions.py``),
not against the whole body:

    echo_vs_question   token / word-3-gram overlap of a chunk window with each question
    ordered_prefix     the chunk starts by repeating a question, in order, before its content
    bait_clusters      chunks that echo the question and each other: one bait, many copies

Thresholds live in ``DocScannerSettings`` with the dev split they were chosen on.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from mailguard.contracts.verdict import ChunkTrust

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_STOPWORD_TEXT = """
a an the of to in on for by with at from as and or but if then is are was were be been being am
do does did doing have has had having i me my we our you your he she it its they them their this
that these those what which who whom whose when where why how can could would should will shall
may might must not no yes so than too very just also any some all there here about into over
under again please hi hello dear thanks thank
"""
STOPWORDS = frozenset(_STOPWORD_TEXT.split())
WINDOW_MIN_TOKENS = 40
"""The opening window holds at least this many content tokens (twice the question when longer)."""
SHORT_QUESTION_WINDOW = 20


def content_tokens(text: str) -> list[str]:
    """Lower-case word tokens without stopwords, in order."""
    return [t for t in _WORD.findall(text.lower()) if t not in STOPWORDS]


def _trigrams(tokens: Sequence[str]) -> set[tuple[str, str, str]]:
    return {(tokens[i], tokens[i + 1], tokens[i + 2]) for i in range(len(tokens) - 2)}


def _echo_one(chunk: Sequence[str], question: Sequence[str], short_tokens: int) -> float:
    if not question:
        return 0.0
    if len(question) <= short_tokens:
        # a short question ("Refund status?") counts only as an exact phrase near the start
        head = list(chunk[:SHORT_QUESTION_WINDOW])
        n = len(question)
        return float(any(head[i : i + n] == list(question) for i in range(len(head) - n + 1)))
    window = chunk[: max(WINDOW_MIN_TOKENS, 2 * len(question))]
    overlap = len(set(question) & set(window)) / len(set(question))
    q3 = _trigrams(question)
    tri = len(q3 & _trigrams(window)) / len(q3) if q3 else 0.0
    return round(0.6 * overlap + 0.4 * tri, 4)


def echo_vs_question(
    chunk_text: str, question_sentences: Sequence[str], *, short_question_tokens: int = 3
) -> float:
    """Max over the questions of how much of the question the chunk's opening window repeats.

    ``0.6 * token overlap + 0.4 * word-3-gram overlap`` of the question's content tokens with
    the first ``max(40, 2 * len(question))`` content tokens of the chunk. A question of at most
    ``short_question_tokens`` content tokens scores 1.0 only as an exact phrase in the first 20
    content tokens, else 0.0, so short questions are neither dropped nor over-matched.
    """
    chunk = content_tokens(chunk_text)
    return max(
        (_echo_one(chunk, content_tokens(q), short_question_tokens) for q in question_sentences),
        default=0.0,
    )


def ordered_prefix(
    chunk_text: str, question_sentences: Sequence[str], *, slack: int = 3, min_tokens: int = 4
) -> bool:
    """True when the chunk opens by repeating a question, its content tokens in order.

    The question's content tokens (at least ``min_tokens``) must form a subsequence of the
    chunk's first ``len(question) + slack`` content tokens: the PoisonedRAG ``question + passage``
    layout, robust to punctuation, casing and a few inserted words.
    """
    chunk = content_tokens(chunk_text)
    for q in question_sentences:
        question = content_tokens(q)
        if len(question) < min_tokens:
            continue
        head = iter(chunk[: len(question) + slack])
        if all(token in head for token in question):
            return True
    return False


def chunk_similarity(a: str, b: str, *, window: int = WINDOW_MIN_TOKENS) -> float:
    """Jaccard similarity of two chunks' opening content tokens."""
    sa, sb = set(content_tokens(a)[:window]), set(content_tokens(b)[:window])
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def bait_clusters(
    chunks: Sequence[tuple[str, str]],
    echo_scores: Mapping[str, float],
    *,
    echo_threshold: float,
    similarity_threshold: float = 0.5,
) -> dict[str, str]:
    """``chunk_id -> cluster_id`` for clusters of at least two high-echo, mutually similar chunks.

    ``chunks`` are ``(chunk_id, content)`` pairs. Union-find over the pairs whose two chunks both
    echo the question at ``echo_threshold`` or more and whose openings are at least
    ``similarity_threshold`` alike (Jaccard). Singletons get no cluster.
    """
    ids = [cid for cid, _ in chunks if echo_scores.get(cid, 0.0) >= echo_threshold]
    text = dict(chunks)
    parent = {cid: cid for cid in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            if chunk_similarity(text[a], text[b]) >= similarity_threshold:
                parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for cid in ids:
        groups.setdefault(find(cid), []).append(cid)
    out: dict[str, str] = {}
    for n, members in enumerate(m for m in groups.values() if len(m) >= 2):
        for cid in members:
            out[cid] = f"bait-{n}"
    return out


def trust_of(score: float, *, quarantine_threshold: float, untrusted_from: float) -> ChunkTrust:
    """``quarantined`` at the quarantine threshold, ``untrusted`` in the suspicious band, else
    ``trusted``."""
    if score >= quarantine_threshold:
        return "quarantined"
    if score >= untrusted_from:
        return "untrusted"
    return "trusted"


__all__ = [
    "STOPWORDS",
    "bait_clusters",
    "chunk_similarity",
    "content_tokens",
    "echo_vs_question",
    "ordered_prefix",
    "trust_of",
]
