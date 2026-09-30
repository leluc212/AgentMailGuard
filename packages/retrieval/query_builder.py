"""Retrieval query construction from email, thread summary, and classification intent.

Requirements:
- R12.1: Construct retrieval query from current email + thread summary + classification intent.
- R12.2: Produce semantic query string and lexical keyword set as separate outputs.
- R12.3: Extract structured identifiers (invoice, order, ticket, SKU, container, incident)
  via configurable regex and pass them to the lexical branch verbatim.
- R12.4: Derive category and metadata filters from classification result.
- R12.5: Construct query without extra LLM calls in the default path.
- R12.6: Persist constructed query with the job for debugging and replay.
- specs/design.md §5.5 & proposal §19: Zero-LLM deterministic query synthesis.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from packages.retrieval.models import RetrievalQuery

if TYPE_CHECKING:
    from packages.domain.entities import Classification, NormalizedMessage

logger = logging.getLogger(__name__)

DEFAULT_IDENTIFIER_PATTERNS: dict[str, str] = {
    "invoice": r"\b(?:INVOICE|INV)(?:[-_# ][A-Z0-9]+(?:[-_][A-Z0-9]+)*|[0-9]{4,15})\b",
    "order": r"\b(?:ORDER|ORD)(?:[-_# ][A-Z0-9]+(?:[-_][A-Z0-9]+)*|[0-9]{4,15})\b",
    "ticket": r"\b(?:TICKET|TICK|TKT)(?:[-_# ][A-Z0-9]+(?:[-_][A-Z0-9]+)*|[0-9]{3,15})\b",
    "sku": r"\bSKU(?:[-_# ][A-Z0-9]+(?:[-_][A-Z0-9]+)*|[0-9]{3,15})\b",
    "container": r"\b(?:CONTAINER|CONT)(?:[-_# ][A-Z0-9]+(?:[-_][A-Z0-9]+)*|[0-9]{4,15})\b",
    "incident": r"\b(?:INCIDENT|INC)(?:[-_# ][A-Z0-9]+(?:[-_][A-Z0-9]+)*|[0-9]{3,15})\b",
}

DEFAULT_STOPWORDS: set[str] = {
    "a",
    "about",
    "above",
    "after",
    "again",
    "against",
    "all",
    "am",
    "an",
    "and",
    "any",
    "are",
    "aren't",
    "as",
    "at",
    "be",
    "because",
    "been",
    "before",
    "being",
    "below",
    "between",
    "both",
    "but",
    "by",
    "can't",
    "cannot",
    "could",
    "couldn't",
    "did",
    "didn't",
    "do",
    "does",
    "doesn't",
    "doing",
    "don't",
    "down",
    "during",
    "each",
    "few",
    "for",
    "from",
    "further",
    "had",
    "hadn't",
    "has",
    "hasn't",
    "have",
    "haven't",
    "having",
    "he",
    "he'd",
    "he'll",
    "he's",
    "her",
    "here",
    "here's",
    "hers",
    "herself",
    "him",
    "himself",
    "his",
    "how",
    "how's",
    "i",
    "i'd",
    "i'll",
    "i'm",
    "i've",
    "if",
    "in",
    "into",
    "is",
    "isn't",
    "it",
    "it's",
    "its",
    "itself",
    "let's",
    "me",
    "more",
    "most",
    "mustn't",
    "my",
    "myself",
    "no",
    "nor",
    "not",
    "of",
    "off",
    "on",
    "once",
    "only",
    "or",
    "other",
    "ought",
    "our",
    "ours",
    "ourselves",
    "out",
    "over",
    "own",
    "same",
    "shan't",
    "she",
    "she'd",
    "she'll",
    "she's",
    "should",
    "shouldn't",
    "so",
    "some",
    "such",
    "than",
    "that",
    "that's",
    "the",
    "their",
    "theirs",
    "them",
    "themselves",
    "then",
    "there",
    "there's",
    "these",
    "they",
    "they'd",
    "they'll",
    "they're",
    "they've",
    "this",
    "those",
    "through",
    "to",
    "too",
    "under",
    "until",
    "up",
    "very",
    "was",
    "wasn't",
    "we",
    "we'd",
    "we'll",
    "we're",
    "we've",
    "were",
    "weren't",
    "what",
    "what's",
    "when",
    "when's",
    "where",
    "where's",
    "which",
    "while",
    "who",
    "who's",
    "whom",
    "why",
    "why's",
    "with",
    "won't",
    "would",
    "wouldn't",
    "you",
    "you'd",
    "you'll",
    "you're",
    "you've",
    "your",
    "yours",
    "yourself",
    "yourselves",
    "hi",
    "hello",
    "thanks",
    "regards",
    "please",
    "dear",
}


# One term is a run of letters and digits, optionally joined by "-", "_", "." or an apostrophe
# ("INV-2026-01829", "v2.1", "don't"). Nothing else can reach the tsquery text, so the terms need
# no escaping and a query that holds tsquery syntax ("&", "|", "!", "(", "'") stays plain words.
_TSQUERY_TERM_RE = re.compile(r"[^\W_]+(?:['\u2019._-][^\W_]+)*")

# A full email is ~20 words after keyword extraction, but the semantic fallback can be ~2000
# characters: cap the OR so PostgreSQL ranks a bounded set of terms.
DEFAULT_MAX_TSQUERY_TERMS = 64


def build_or_tsquery(
    text: str,
    *,
    stopwords: Collection[str] = DEFAULT_STOPWORDS,
    max_terms: int = DEFAULT_MAX_TSQUERY_TERMS,
) -> str:
    """Build the OR of a query's terms as text for ``to_tsquery('english', ...)`` (G.1, R10.1).

    ``websearch_to_tsquery`` ANDs every word, so a ~20-word email query matched almost no chunk
    and hybrid retrieval was vector-only. Here every term is an alternative: a chunk matches when
    it holds any of them, and ``ts_rank_cd`` ranks the chunks that hold more of them higher.

    Terms are lowercased, stop words and one-character terms are dropped, duplicates are removed
    in first-seen order and at most ``max_terms`` are kept. Each term is single-quoted so that
    PostgreSQL tokenizes it as it tokenized the chunk (an identifier such as ``INV-2026-01829``
    matches the chunk that holds it) and stems it. Returns ``""`` when nothing usable is left, and
    the caller skips the lexical branch.
    """
    seen: set[str] = set()
    terms: list[str] = []
    for match in _TSQUERY_TERM_RE.finditer(text or ""):
        raw = match.group(0).lower().replace("\u2019", "'")
        if raw in stopwords:
            continue
        term = re.sub(r"'s$", "", raw).replace("'", "")
        if len(term) < 2 or term in stopwords or term in seen:
            continue
        seen.add(term)
        terms.append(f"'{term}'")
        if len(terms) >= max_terms:
            break
    return " | ".join(terms)


@dataclass
class QueryBuilderConfig:
    """Configuration for query construction and identifier extraction."""

    identifier_patterns: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_IDENTIFIER_PATTERNS)
    )
    stopwords: set[str] = field(default_factory=lambda: set(DEFAULT_STOPWORDS))
    max_lexical_keywords: int = 20
    max_body_semantic_chars: int = 2000
    category_filter_enabled: bool = True
    ignored_categories_for_filter: set[str] = field(
        default_factory=lambda: {"general", "other", "unknown"}
    )


class RetrievalQueryBuilder:
    """Builds RetrievalQuery from email, thread summary, and intent.

    Fulfills R12.1-R12.6. Executes synchronously in sub-millisecond time
    without LLM calls in the default path.
    """

    def __init__(self, config: QueryBuilderConfig | None = None) -> None:
        self.config = config or QueryBuilderConfig()
        self._compiled_patterns: list[tuple[str, re.Pattern[str]]] = [
            (name, re.compile(pat, re.IGNORECASE))
            for name, pat in self.config.identifier_patterns.items()
        ]

    def extract_identifiers(self, *texts: str) -> list[str]:
        """Extract structured identifiers across provided text snippets (R12.3).

        Deduplicates while preserving first-seen order.
        """
        seen: set[str] = set()
        identifiers: list[str] = []

        for text in texts:
            if not text:
                continue
            for _name, pattern in self._compiled_patterns:
                for match in pattern.finditer(text):
                    cleaned = match.group(0).strip().upper()
                    if cleaned and cleaned not in seen:
                        seen.add(cleaned)
                        identifiers.append(cleaned)

        return identifiers

    def extract_lexical_keywords(self, *texts: str) -> list[str]:
        """Extract salient keywords excluding stopwords and identifiers (R12.2)."""
        seen: set[str] = set()
        keywords: list[str] = []

        # Tokenize alphanumeric words (min 2 chars, letters, digits, hyphen)
        word_re = re.compile(r"\b[a-zA-Z0-9][a-zA-Z0-9\-_]{1,30}\b")

        for text in texts:
            if not text:
                continue
            for match in word_re.finditer(text.lower()):
                word = match.group(0).strip()
                if (
                    len(word) >= 2
                    and word not in self.config.stopwords
                    and not word.isdigit()
                    and word not in seen
                ):
                    seen.add(word)
                    keywords.append(word)
                    if len(keywords) >= self.config.max_lexical_keywords:
                        return keywords

        return keywords

    def compose_semantic_text(
        self,
        *,
        subject: str,
        body: str,
        intent: str | None = None,
        thread_summary: str | None = None,
    ) -> str:
        """Compose semantic query string from email + thread summary + intent (R12.1)."""
        parts: list[str] = []

        if intent and intent.strip():
            parts.append(f"Intent: {intent.strip()}.")

        if thread_summary and thread_summary.strip():
            parts.append(f"Thread summary: {thread_summary.strip()}.")

        if subject and subject.strip():
            parts.append(f"Subject: {subject.strip()}.")

        if body and body.strip():
            # Truncate body if excessively long to prevent token overflow on dense embedder
            body_clean = body.strip()
            if len(body_clean) > self.config.max_body_semantic_chars:
                body_clean = body_clean[: self.config.max_body_semantic_chars] + "..."
            parts.append(f"Body: {body_clean}")

        semantic = " ".join(parts).strip()
        # Collapse multiple spaces or newlines into clean readable text
        return re.sub(r"\s+", " ", semantic)

    def build(
        self,
        message: NormalizedMessage | None = None,
        classification: Classification | dict[str, Any] | None = None,
        *,
        subject: str = "",
        body_text: str = "",
        intent: str | None = None,
        category: str | None = None,
        organization_id: str | None = None,
        thread_summary: str | None = None,
        extra_filters: dict[str, Any] | None = None,
    ) -> RetrievalQuery:
        """Build RetrievalQuery from inputs.

        Accepts either high-level domain entities (NormalizedMessage, Classification)
        or primitive arguments, combining them deterministically (R12.1-R12.6).
        """
        # Resolve subject and body
        eff_subject = (
            message.subject_normalized or message.subject if message else subject
        ).strip()
        eff_body = (message.body_text_clean or message.body_text if message else body_text).strip()

        # Resolve organization_id
        eff_org_id = organization_id
        if not eff_org_id and message:
            eff_org_id = str(message.organization_id)

        # Resolve intent and category
        eff_intent = intent
        eff_category = category
        if classification is not None:
            if isinstance(classification, dict):
                eff_intent = classification.get("intent") or eff_intent
                eff_category = classification.get("category") or eff_category
            else:
                eff_intent = getattr(classification, "intent", None) or eff_intent
                eff_category = getattr(classification, "category", None) or eff_category

        # 1. Extract structured identifiers (R12.3)
        identifiers = self.extract_identifiers(eff_subject, eff_body)

        # 2. Extract discrete lexical keywords (R12.2)
        # Keywords combine intent, subject, and body
        lexical_sources = [eff_intent or "", eff_subject, eff_body]
        lexical_terms = self.extract_lexical_keywords(*lexical_sources)

        # 3. Compose semantic query text (R12.1)
        semantic_text = self.compose_semantic_text(
            subject=eff_subject,
            body=eff_body,
            intent=eff_intent,
            thread_summary=thread_summary,
        )

        # 4. Synthesize filters (R12.4)
        filters: dict[str, Any] = {"status": "active"}
        if eff_org_id:
            filters["organization_id"] = eff_org_id

        if (
            self.config.category_filter_enabled
            and eff_category
            and eff_category.lower() not in self.config.ignored_categories_for_filter
        ):
            filters["category"] = eff_category.lower()

        if extra_filters:
            filters.update(extra_filters)

        return RetrievalQuery(
            semantic_text=semantic_text,
            lexical_terms=lexical_terms,
            identifiers=identifiers,
            filters=filters,
        )
