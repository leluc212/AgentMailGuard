"""Benchmark case -> rag-email host objects (task 7.19; spec §4 steps 1-2; ADR-0010).

    BenchCase line ─▶ EvalCase ─▶ throwaway organization ─▶ case KB docs ingested
                                     │                       (KnowledgeIngestionPipeline)
                                     ▼
            NormalizedMessage + Classification ─▶ ContextBuilder.build_context
                                                   (real HybridRetriever over Postgres)

The runner never puts a chunk into the prompt itself. Every case knowledge document,
poisoned or not, is ingested, and only what retrieval returns reaches the ContextPackage.
The builder has no business-data provider, so ``business_data`` is always None. That also
keeps AgentMailGuard's adapter off its ``dict(BusinessContext)`` path. Nothing here is
defence logic.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from packages.context.assembly import ThreadContextAssembler
from packages.context.builder import ContextBuilder
from packages.core.settings import AppSettings
from packages.db.knowledge import PostgresKnowledgeStore
from packages.domain.entities import (
    Classification,
    ContextPackage,
    EmailAddress,
    Job,
    NormalizedMessage,
)
from packages.domain.knowledge import KnowledgeDocument
from packages.knowledge.embedder import Embedder
from packages.knowledge.pipeline import KnowledgeIngestionPipeline
from packages.llm.profile import AgentProfileRegistry
from packages.retrieval.postgres import PostgresSearchBackend
from packages.retrieval.retriever import HybridRetriever

EVAL_ORG_PREFIX = "mailguard-bench"
"""Name prefix of every throwaway organization; purge_stale_eval_orgs matches on it."""
EVAL_PROVIDER = "mailguard-bench"
EVAL_RECIPIENT = "support@mailguard-bench.invalid"
DEFAULT_CATEGORY = "general_inquiry"
KB_DOC_TITLE = "Knowledge article"
"""Neutral title for every case document, so no 'poisoned'/'clean' id leaks into the prompt."""
CASE_KINDS = frozenset({"attack", "benign"})


class KbIngestionError(RuntimeError):
    """A case knowledge document did not end as an active document with chunks."""


@dataclass(frozen=True)
class CaseEmail:
    sender_email: str
    sender_name: str
    subject: str
    body_text: str


@dataclass(frozen=True)
class KbDoc:
    """One case knowledge chunk, ingested as its own rag-email knowledge document."""

    chunk_id: str
    content: str
    poisoned: bool


@dataclass(frozen=True)
class EvalCase:
    """An AgentMailGuard ``BenchCase`` (evaluation/harness.py) as rag-email reads it."""

    case_id: str
    kind: str
    source: str
    technique: str | None
    vector: str
    category: str
    email: CaseEmail
    kb_docs: tuple[KbDoc, ...]
    kb_query: str
    goal: Mapping[str, Any]
    attacker: Mapping[str, str]
    expected_keywords: tuple[str, ...]
    meta: Mapping[str, Any]

    @property
    def scenario(self) -> str | None:
        """LLMail-Inject scenario (for example ``level2v``); None for other sources."""
        value = self.meta.get("scenario")
        return str(value) if value else None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> EvalCase:
        """Parse one BenchCase JSON object.

        Raises:
            ValueError: If the id, kind or email body is missing, or a KB chunk is empty.
        """
        case_id = str(raw.get("case_id") or "").strip()
        if not case_id:
            raise ValueError("benchmark case has no case_id")
        kind = str(raw.get("kind") or "")
        if kind not in CASE_KINDS:
            raise ValueError(f"case {case_id}: kind must be attack or benign, got {kind!r}")
        email_raw = raw.get("email") or {}
        if not isinstance(email_raw, Mapping):
            raise ValueError(f"case {case_id}: email must be an object")
        body = str(email_raw.get("body_text") or email_raw.get("body") or "")
        if not body.strip():
            raise ValueError(f"case {case_id}: email body is empty")

        docs: list[KbDoc] = []
        for chunk in raw.get("chunks") or []:
            metadata = chunk.get("metadata") or {}
            content = str(chunk.get("content") or chunk.get("text") or "").strip()
            chunk_id = str(chunk.get("chunk_id") or chunk.get("id") or f"{case_id}-kb{len(docs)}")
            if not content:
                raise ValueError(f"case {case_id}: KB chunk {chunk_id!r} is empty")
            poisoned = bool(chunk.get("poisoned", metadata.get("poisoned", False)))
            docs.append(KbDoc(chunk_id=chunk_id, content=content, poisoned=poisoned))

        technique = raw.get("technique")
        return cls(
            case_id=case_id,
            kind=kind,
            source=str(raw.get("source") or "unknown"),
            technique=str(technique) if technique else None,
            vector=str(raw.get("vector") or "email"),
            category=str(raw.get("category") or email_raw.get("category") or ""),
            email=CaseEmail(
                sender_email=str(
                    email_raw.get("sender_email") or "unknown@mailguard-bench.invalid"
                ),
                sender_name=str(email_raw.get("sender_name") or ""),
                subject=str(email_raw.get("subject") or ""),
                body_text=body,
            ),
            kb_docs=tuple(docs),
            kb_query=str(raw.get("kb_query") or ""),
            goal=dict(raw.get("goal") or {}),
            attacker={str(k): str(v) for k, v in (raw.get("attacker") or {}).items()},
            expected_keywords=tuple(str(k) for k in raw.get("expected_keywords") or []),
            meta=dict(raw.get("meta") or {}),
        )


def classification_for(case: EvalCase) -> Classification:
    """Fixed classification from the case category. Triage is not under test here."""
    category = case.category.strip().lower() or DEFAULT_CATEGORY
    return Classification(
        category=category,
        intent=None,
        reply_required=True,
        workflow_hint="ai",
        retrieval_required=True,
        confidence=1.0,
        decided_by="benchmark_case",
    )


def to_normalized_message(
    case: EvalCase, *, organization_id: UUID, received_at: datetime | None = None
) -> NormalizedMessage:
    """The case email as the provider-neutral message rag-email's pipeline consumes."""
    email = case.email
    return NormalizedMessage(
        message_id=uuid4(),
        thread_id=uuid4(),
        mailbox_id=uuid4(),
        organization_id=organization_id,
        provider=EVAL_PROVIDER,
        provider_message_id=case.case_id,
        sender=EmailAddress(email=email.sender_email, name=email.sender_name or None),
        received_at=received_at or datetime.now(UTC),
        recipients=[EmailAddress(email=EVAL_RECIPIENT)],
        subject=email.subject,
        subject_normalized=email.subject.strip(),
        body_text=email.body_text,
        body_text_clean=email.body_text,
        snippet=email.body_text[:200],
        direction="inbound",
    )


@asynccontextmanager
async def eval_organization(pool: asyncpg.Pool[Any], *, label: str) -> AsyncIterator[UUID]:
    """A throwaway organization, deleted on exit whatever happens (scripts/phase5_gate.py).

    knowledge_document, knowledge_chunk and embedding_record reference organization(id)
    ON DELETE CASCADE (migrations/0001_core_schema.up.sql), so one DELETE cleans up.
    """
    org_id = uuid4()
    name = f"{EVAL_ORG_PREFIX} {label}"[:200]
    await pool.execute("INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, name)
    try:
        yield org_id
    finally:
        await pool.execute("DELETE FROM organization WHERE id = $1", org_id)


def _like_literal(text: str) -> str:
    """Escape LIKE wildcards so a run name such as ``full_run`` matches only itself."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def purge_stale_eval_orgs(pool: asyncpg.Pool[Any], *, scope: str) -> int:
    """Delete this RUN/CONFIG's organizations left behind by a killed run. Returns the count.

    Scoped on purpose: another config may be running in a second terminal, and a
    global purge would cascade-delete the knowledge base of the case it has in flight.
    The runner holds a per-RUN/CONFIG advisory lock (runner.py), so nothing else owns
    orgs under this scope while the purge runs.
    """
    status = await pool.execute(
        "DELETE FROM organization WHERE name LIKE $1 ESCAPE '\\'",
        f"{EVAL_ORG_PREFIX} {_like_literal(scope)} %",
    )
    return int(status.split()[-1])


@dataclass(frozen=True)
class IngestedDoc:
    rag_document_id: str
    case_chunk_id: str
    poisoned: bool


async def ingest_case_kb(
    pool: asyncpg.Pool[Any],
    embedder: Embedder,
    case: EvalCase,
    *,
    organization_id: UUID,
    category: str,
) -> dict[str, IngestedDoc]:
    """Ingest each case KB chunk as one markdown document through the real pipeline.

    Raises:
        KbIngestionError: If a document does not end ``active`` with at least one chunk.
    """
    store = PostgresKnowledgeStore(pool)
    pipeline = KnowledgeIngestionPipeline(store=store, embedder=embedder)
    ingested: dict[str, IngestedDoc] = {}
    for doc in case.kb_docs:
        record = KnowledgeDocument(
            organization_id=organization_id,
            title=KB_DOC_TITLE,
            category=category,
            mime_type="text/markdown",
            status="pending",
        )
        await store.insert_document(record)
        result = await pipeline.ingest_document(
            record.id,
            organization_id,
            raw_bytes=doc.content.encode("utf-8"),
            filename="article.md",
            content_type="text/markdown",
        )
        if result.status != "active" or result.total_chunks < 1:
            raise KbIngestionError(
                f"case {case.case_id}: KB doc {doc.chunk_id} ended {result.status!r} "
                f"with {result.total_chunks} chunks"
            )
        ingested[str(record.id)] = IngestedDoc(
            rag_document_id=str(record.id), case_chunk_id=doc.chunk_id, poisoned=doc.poisoned
        )
    return ingested


@dataclass(frozen=True)
class RetrievedRef:
    """One chunk retrieval put into the ContextPackage, mapped back to the case."""

    rank: int
    rag_chunk_id: str
    rag_document_id: str
    case_chunk_id: str | None
    poisoned: bool


@dataclass(frozen=True)
class PreparedCase:
    case: EvalCase
    organization_id: UUID
    message: NormalizedMessage
    classification: Classification
    context: ContextPackage
    retrieval_query: str
    ingested: Mapping[str, IngestedDoc]
    retrieved: tuple[RetrievedRef, ...]
    context_ms: int

    @property
    def poison_ingested(self) -> bool:
        return any(doc.poisoned for doc in self.ingested.values())

    @property
    def poison_retrieved(self) -> bool:
        return any(ref.poisoned for ref in self.retrieved)

    def diagnostics(self) -> dict[str, Any]:
        """Host-side facts for the result row (retrieval misses are logged, not hidden)."""
        return {
            "classification_category": self.classification.category,
            "retrieval_query": self.retrieval_query,
            "kb_docs_ingested": len(self.ingested),
            "poison_ingested": self.poison_ingested,
            "poison_retrieved": self.poison_retrieved,
            "retrieved": [asdict(ref) for ref in self.retrieved],
            "context_ms": self.context_ms,
        }


@dataclass
class EvalHost:
    """rag-email's context path wired in-process, as services/ai_worker/main.py wires it.

    Differences from the worker, all deliberate: no job store (nothing is written to
    processing_job), no thread stores (single-email cases, thread_messages=[]), and no
    business-data provider (the evaluation org has no business data).
    """

    pool: asyncpg.Pool[Any]
    embedder: Embedder
    context_builder: ContextBuilder

    @classmethod
    def create(
        cls,
        settings: AppSettings,
        *,
        pool: asyncpg.Pool[Any],
        embedder: Embedder,
        profile_registry: AgentProfileRegistry,
    ) -> EvalHost:
        retrieval = settings.retrieval
        retriever = HybridRetriever(
            PostgresSearchBackend(pool),
            timeout_seconds=retrieval.retrieval_timeout_ms / 1000,
            default_top_n=retrieval.top_n,
            rrf_k=retrieval.rrf_k,
            embedder=embedder,
        )
        builder = ContextBuilder(
            thread_assembler=ThreadContextAssembler(settings=settings.summarization),
            retriever=retriever,
            business_data_provider=None,
            job_store=None,
            top_k=retrieval.top_k,
            profile_registry=profile_registry,
        )
        return cls(pool=pool, embedder=embedder, context_builder=builder)

    async def prepare(self, case: EvalCase, *, organization_id: UUID) -> PreparedCase:
        """Ingest the case KB, then build the real ContextPackage for the case email."""
        classification = classification_for(case)
        ingested = await ingest_case_kb(
            self.pool,
            self.embedder,
            case,
            organization_id=organization_id,
            category=classification.category,
        )
        message = to_normalized_message(case, organization_id=organization_id)
        job = Job(
            organization_id=organization_id,
            message_id=message.message_id,
            thread_id=message.thread_id,
        )
        started = time.perf_counter()
        context = await self.context_builder.build_context(
            job, message, classification, thread_messages=[], thread_state=None
        )
        context_ms = int((time.perf_counter() - started) * 1000)
        if context.business_data is not None:
            raise RuntimeError("benchmark host must not carry business data")
        # The same builder and inputs build_context used, so this is the query retrieval ran.
        query = self.context_builder.query_builder.build(
            message=message, classification=classification, thread_summary=context.thread_summary
        )
        retrieved = tuple(
            RetrievedRef(
                rank=index,
                rag_chunk_id=str(chunk.chunk_id),
                rag_document_id=str(chunk.document_id),
                case_chunk_id=(
                    ingested[str(chunk.document_id)].case_chunk_id
                    if str(chunk.document_id) in ingested
                    else None
                ),
                poisoned=(
                    str(chunk.document_id) in ingested and ingested[str(chunk.document_id)].poisoned
                ),
            )
            for index, chunk in enumerate(context.retrieved_chunks, start=1)
        )
        return PreparedCase(
            case=case,
            organization_id=organization_id,
            message=message,
            classification=classification,
            context=context,
            retrieval_query=query.semantic_text,
            ingested=ingested,
            retrieved=retrieved,
            context_ms=context_ms,
        )
