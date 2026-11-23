"""The customer's own questions in an email body, as a pure function (ADR-0015 decision 8).

PoisonedRAG bait restates the question the attacker expects; the Doc Scanner (Task 7) compares
retrieved chunks with these sentences instead of with the whole body. The function is not part
of the Inbound Guard: the pipeline calls it for every email, so removing L1 (ablation C3-L1)
does not also weaken the Doc Scanner.

A sentence counts when it ends in ``?`` or starts with a request phrase ("please", "could
you", "I need", ...). Quoted history (``>`` lines, everything from an "On ... wrote:" line or a
forwarded/original-message header) is dropped first. At most ``MAX_SENTENCES``, in order,
de-duplicated; each is cut to ``MAX_TOKENS`` whitespace tokens. Short questions ("Refund
status?") are kept.
"""

from __future__ import annotations

import re

MAX_SENTENCES = 8
MAX_TOKENS = 40

_QUOTE_HEADER = re.compile(
    r"^\s*(?:on\s.{1,200}\swrote:|-{2,}\s*(?:original message|forwarded message)\s*-{2,}"
    r"|from:\s.+@.+|begin forwarded message:)\s*$",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?]+|$)")
_REQUEST_START = re.compile(
    r"^(?:please|kindly|could you|can you|would you|will you|i need|i want|i would like|"
    r"i'd like|help me|how (?:do|can|should) i|tell me|let me know)\b",
    re.IGNORECASE,
)


def strip_quoted_history(body: str) -> str:
    """The body without ``>``-quoted lines and without anything after a reply/forward header."""
    kept: list[str] = []
    for line in body.splitlines():
        if _QUOTE_HEADER.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _clip(sentence: str) -> str:
    tokens = sentence.split()
    return " ".join(tokens[:MAX_TOKENS])


def question_sentences(body: str) -> list[str]:
    """Questions and requests of ``body``, in order (pure; ``[]`` when there are none)."""
    out: list[str] = []
    for match in _SENTENCE.finditer(strip_quoted_history(body or "")):
        sentence = " ".join(match.group(0).split())
        if not sentence:
            continue
        if sentence.endswith("?") or _REQUEST_START.match(sentence):
            clipped = _clip(sentence)
            if clipped not in out:
                out.append(clipped)
        if len(out) >= MAX_SENTENCES:
            break
    return out


__all__ = ["MAX_SENTENCES", "MAX_TOKENS", "question_sentences", "strip_quoted_history"]
