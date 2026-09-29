# Citation Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject citations in a generated draft that name knowledge chunks absent from the assembled context, flag the draft `citation_mismatch`, and export the counters a mismatch rate is computed from.

**Architecture:** A pure verifier in `packages/llm/citations.py` builds an index of every citation id the prompt template could have shown the model — `external_id`, falling back to `chunk_id` — and partitions the draft's `knowledge_chunks` into accepted citations and mismatches. `SinglePassGenerator` runs it after schema validation succeeds and attaches the verdict to `GenerationResult`. A mismatch is a quality flag, **not** a job failure: the draft is still returned, because R16.3 governs schema failure and R16.5 governs grounding.

**Tech Stack:** Python 3.12, dataclasses, `prometheus_client`, pytest + pytest-asyncio, ruff, mypy strict.

**Spec:** `specs/tasks.md` task 4.10; `specs/requirements.md` R16.5 (with R21.4 for metric conventions); `specs/design.md` §5.7 line 618 and §6.1 lines 742–762; `docs/proposal/Technical Proposal — Enterprise RAG-Based Intelligent Email Management and Response System.md` §24 (source of truth for the response schema and the `DOC-125-08` citation id form).

**Execution:** Subagent-driven (chosen by the user at plan review on 2026-09-26): a fresh implementer per task, a fresh reviewer gating each one, then a whole-branch review.

## Global Constraints

- Python `>=3.12`; `uv` for every command. Run all commands from the repo root.
- `packages/*` must never import `services/*`; `packages/domain` imports stdlib + `packages/core` only. (`CLAUDE.md` §4)
- Provider names (`gmail`, `graph`, `imap`) appear only inside `packages/adapters/`. (`CLAUDE.md` §4)
- `ruff check` and `ruff format --check` clean on every touched file; `mypy packages/` clean under `strict = true`. Line length 100.
- Metric labels stay low-cardinality (`specs/design.md` line 267). `category` is bounded by `config/categories.yaml`; never label a metric with a chunk id, message id, or draft body.
- New config keys go in `.env.example` **and** `docs/configuration.md`. **This task adds no config keys** — the verifier has no thresholds and is always on.
- Commit messages carry the task number and requirement IDs: `feat(llm): ... [task 4.10] [R16.5]`.
- Definition of Done per `CLAUDE.md` §3. Do not mark `[x]` on partial work — mark `[~]` and say what is left.
- Every commit message ends with: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

## Decisions taken from the spec (read these before Task 1)

**D1 — The citation identifier space is `external_id`, with a `chunk_id` fallback, and both are accepted.** *(Confirmed by the user at plan review on 2026-09-26, over the stricter external-id-only alternative.)*
Evidence: `prompts/support.v1.j2` line 32 renders `[CITATION: {{ chunk.external_id or chunk.chunk_id }}]` and line 46 instructs "Every cited chunk must be included in `knowledge_chunks` with its exact citation ID"; `packages/domain/knowledge.py:150` comments `external_id` as `# e.g. "DOC-125-08" for citations (R16.5)`; `specs/design.md:762` says `external_id TEXT, -- "DOC-125-08", used in citations`; proposal §24 shows `["DOC-125-08", "DOC-772-02"]`.
The verifier accepts **either** alias of a supplied chunk. R16.5's test is correspondence to "chunks actually supplied in the context" — a `chunk_id` of a supplied chunk does correspond to it. Rejecting the alias would flag the repo's own existing fixtures (`tests/unit/test_single_pass_generator.py` cites `chunk-1` while that Candidate's `external_id` is `KB-PWD-01`) and inflate the mismatch rate with false positives, corrupting the metric the requirement exists to produce.

**D2 — `document_id` is deliberately NOT accepted.** Several chunks share a `document_id`, so citing one is not evidence that any particular chunk was supplied. R16.5 is chunk-level. This is Review Focus item 5.

**D3 — A mismatch flags the draft; it never fails the job.** R16.5 says "recording a `citation_mismatch` flag", and `generated_draft.citation_mismatch BOOLEAN NOT NULL DEFAULT false` already exists in `migrations/0001_core_schema.up.sql:179`. Failing the job would be R16.3 behaviour, which is a different criterion.

**D4 — The rate is exported as two counters, not a gauge.** `specs/tasks.md` 4.10 says "export its rate as a metric" and `design.md:618` says "its rate is a reportable quality metric". A Prometheus rate is derived from monotonic counters: `rate(citation_mismatches_total[5m]) / rate(citations_verified_total[5m])`. A gauge would require per-process accumulator state, which contradicts the stateless-worker principle (proposal §5.5, `specs/design.md` §5.8). The PromQL expression is documented in `docs/observability.md` as part of Task 2, and the Grafana panel belongs to Phase 7 (task 7.4), not here.

**D5 — `GenerationResult.content` is left as the validated payload.** The verdict is carried separately, so the record of what the model actually claimed is preserved for debugging. Task 4.11 (draft persistence) must persist `verdict.citations` into `generated_draft.citations` and `verdict.mismatch` into `generated_draft.citation_mismatch` — **not** `content["knowledge_chunks"]`, which still contains the rejected ids. This is stated in Task 3's Produces block.

## Review Focus

- A cited id differing from the supplied one only by case or surrounding whitespace (`" doc-125-08 "` vs `DOC-125-08`) must match, not count as a hallucination — pinned by Task 1 Step 9.
- The same chunk cited twice must count once, so the cited-count denominator is not inflated — pinned by Task 1 Step 11.
- A chunk whose `external_id` is `None` is shown to the model as its `chunk_id`, so citing that `chunk_id` must match — pinned by Task 1 Step 13.
- A draft citing ids when **zero** chunks were supplied must mark every citation a mismatch and must not divide by zero anywhere — pinned by Task 1 Step 15 and Task 3 Step 11.
- A citation naming a `document_id` must NOT match, since a document id is not evidence a specific chunk was supplied (D2) — pinned by Task 1 Step 17.

---

### Task 1: Citation verifier

**Files:**
- Create: `packages/llm/citations.py`
- Modify: `packages/llm/__init__.py` (add re-exports to the existing import block and `__all__`)
- Test: `tests/unit/test_citation_verification.py`

**Interfaces:**
- Consumes: `packages.domain.entities.Candidate` (fields `chunk_id: str`, `document_id: str`, `content: str`, `external_id: str | None`) and `packages.domain.entities.ContextPackage` (field `retrieved_chunks: list[Candidate]`). Both already exist; do not modify them.
- Produces:
  - `CitationVerdict` — frozen dataclass with `citations: list[dict[str, Any]]`, `mismatched: list[str]`, `supplied_count: int`, `cited_count: int`, and properties `mismatch: bool` and `mismatch_ratio: float`.
  - `build_citation_index(chunks: Sequence[Candidate]) -> dict[str, Candidate]`
  - `verify_citations(cited_ids: Sequence[str], context: ContextPackage) -> CitationVerdict`
  - Each dict in `citations` has exactly the keys `citation_id`, `chunk_id`, `document_id`, `external_id`.
  - No other public name. (Pre-flight ruling: a `CITATION_KEYS` constant was cut from this plan — nothing consumed it.)

- [ ] **Step 1: Write the failing test for the accepted-citation shape**

Create `tests/unit/test_citation_verification.py`:

```python
"""Unit tests for citation grounding verification (R16.5, design.md §5.7)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from packages.domain.entities import (
    Candidate,
    ContextPackage,
    EmailAddress,
    NormalizedMessage,
)
from packages.llm import CitationVerdict, build_citation_index, verify_citations


def _chunk(
    chunk_id: str,
    external_id: str | None = None,
    document_id: str = "doc-kb-01",
) -> Candidate:
    return Candidate(
        chunk_id=chunk_id,
        document_id=document_id,
        content="To reset a password, open settings and choose Reset Password.",
        external_id=external_id,
    )


def _context(*chunks: Candidate) -> ContextPackage:
    message = NormalizedMessage(
        message_id=uuid4(),
        organization_id=uuid4(),
        mailbox_id=uuid4(),
        thread_id=uuid4(),
        provider="mock",
        provider_message_id="msg-cite-001",
        sender=EmailAddress(email="customer@example.com", name="Alice Customer"),
        subject="How do I reset my password?",
        subject_normalized="How do I reset my password?",
        body_text="I forgot my password.",
        body_text_clean="I forgot my password.",
        received_at=datetime.now(UTC),
    )
    return ContextPackage(
        agent_instructions="You are an enterprise AI assistant.",
        category_instructions="Address technical support questions.",
        current_message=message,
        retrieved_chunks=list(chunks),
    )


def test_citation_naming_a_supplied_chunk_is_accepted() -> None:
    """A citation matching a supplied chunk's external_id resolves to that chunk."""
    context = _context(_chunk("chunk-1", external_id="DOC-125-08"))

    verdict = verify_citations(["DOC-125-08"], context)

    assert isinstance(verdict, CitationVerdict)
    assert verdict.mismatch is False
    assert verdict.mismatched == []
    assert verdict.supplied_count == 1
    assert verdict.cited_count == 1
    assert verdict.citations == [
        {
            "citation_id": "DOC-125-08",
            "chunk_id": "chunk-1",
            "document_id": "doc-kb-01",
            "external_id": "DOC-125-08",
        }
    ]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/test_citation_verification.py -p no:cacheprovider`
Expected: FAIL at collection — `ImportError: cannot import name 'CitationVerdict' from 'packages.llm'`.

- [ ] **Step 3: Write the verifier**

Create `packages/llm/citations.py`:

```python
"""Citation grounding verification for generated drafts (R16.5, design.md §5.7).

The hallucinated-citation detector. Every id a draft cites must name a knowledge chunk that
was actually supplied in the ContextPackage; citations that do not are rejected and the draft
is flagged `citation_mismatch`. A mismatch is a quality signal, not a job failure — an
unparseable or schema-invalid draft is R16.3's concern, handled in `packages/llm/validation.py`.

The accepted identifier space follows what the prompt template actually shows the model:
`prompts/*.j2` renders `[CITATION: {{ chunk.external_id or chunk.chunk_id }}]`, so both
aliases of a supplied chunk are accepted. `document_id` is not: several chunks share one, so
citing a document is not evidence that any particular chunk was supplied.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from packages.domain.entities import Candidate, ContextPackage

@dataclass(frozen=True)
class CitationVerdict:
    """Outcome of checking a draft's citations against the context it was generated from."""

    citations: list[dict[str, Any]] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    supplied_count: int = 0
    cited_count: int = 0

    @property
    def mismatch(self) -> bool:
        """True when at least one citation named no supplied chunk (R16.5)."""
        return bool(self.mismatched)

    @property
    def mismatch_ratio(self) -> float:
        """Share of distinct citations that named no supplied chunk, 0.0 when none were made."""
        if self.cited_count == 0:
            return 0.0
        return len(self.mismatched) / self.cited_count


def _normalize(citation_id: str) -> str:
    """Fold a citation id for comparison, absorbing case drift and stray whitespace."""
    return citation_id.strip().casefold()


def build_citation_index(chunks: Sequence[Candidate]) -> dict[str, Candidate]:
    """Map every citation id the prompt could have shown the model to its chunk.

    The first chunk claiming an alias wins, so a duplicated id resolves deterministically.
    """
    index: dict[str, Candidate] = {}
    for chunk in chunks:
        for alias in (chunk.external_id, chunk.chunk_id):
            if not alias:
                continue
            index.setdefault(_normalize(alias), chunk)
    return index


def verify_citations(
    cited_ids: Sequence[str],
    context: ContextPackage,
) -> CitationVerdict:
    """Partition a draft's citations into ones grounded in the context and ones that are not.

    Args:
        cited_ids: The draft's `knowledge_chunks` field, as validated by DraftReplyPayload.
        context: The ContextPackage the draft was generated from.

    Returns:
        A CitationVerdict whose `citations` hold only accepted citations, resolved to their
        chunk, and whose `mismatched` hold the rejected ids as the model wrote them.
    """
    index = build_citation_index(context.retrieved_chunks)
    accepted: list[dict[str, Any]] = []
    mismatched: list[str] = []
    seen: set[str] = set()

    for raw in cited_ids:
        key = _normalize(raw)
        if not key or key in seen:
            continue
        seen.add(key)

        chunk = index.get(key)
        if chunk is None:
            mismatched.append(raw.strip())
            continue

        accepted.append(
            {
                "citation_id": raw.strip(),
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "external_id": chunk.external_id,
            }
        )

    return CitationVerdict(
        citations=accepted,
        mismatched=mismatched,
        supplied_count=len(context.retrieved_chunks),
        cited_count=len(seen),
    )
```

- [ ] **Step 4: Add the re-exports**

In `packages/llm/__init__.py`, add this import block immediately after the `from packages.llm.budget import (...)` block:

```python
from packages.llm.citations import (
    CitationVerdict,
    build_citation_index,
    verify_citations,
)
```

Then add these four entries to `__all__`, keeping it alphabetically sorted:

```python
    "CitationVerdict",
    "build_citation_index",
    "verify_citations",
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/unit/test_citation_verification.py -p no:cacheprovider`
Expected: PASS, 1 passed.

- [ ] **Step 6: Write the failing test for a hallucinated citation**

Append to `tests/unit/test_citation_verification.py`:

```python
def test_citation_naming_no_supplied_chunk_is_rejected() -> None:
    """An id absent from the context is rejected and sets the mismatch flag (R16.5)."""
    context = _context(_chunk("chunk-1", external_id="DOC-125-08"))

    verdict = verify_citations(["DOC-999-99"], context)

    assert verdict.mismatch is True
    assert verdict.mismatched == ["DOC-999-99"]
    assert verdict.citations == []
    assert verdict.cited_count == 1
    assert verdict.mismatch_ratio == 1.0


def test_accepted_and_rejected_citations_are_partitioned() -> None:
    """A draft mixing a real and an invented citation keeps the real one and flags the draft."""
    context = _context(
        _chunk("chunk-1", external_id="DOC-125-08"),
        _chunk("chunk-2", external_id="DOC-772-02", document_id="doc-kb-02"),
    )

    verdict = verify_citations(["DOC-125-08", "DOC-000-00", "DOC-772-02"], context)

    assert verdict.mismatch is True
    assert verdict.mismatched == ["DOC-000-00"]
    assert [entry["external_id"] for entry in verdict.citations] == ["DOC-125-08", "DOC-772-02"]
    assert verdict.cited_count == 3
    assert verdict.mismatch_ratio == pytest.approx(1 / 3)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_citation_verification.py -p no:cacheprovider`
Expected: PASS, 3 passed. These pass on first run — they pin behaviour Step 3 already implemented, and the RED for that behaviour was Step 2's ImportError.

- [ ] **Step 8: Commit**

```bash
git add packages/llm/citations.py packages/llm/__init__.py tests/unit/test_citation_verification.py
git commit -m "feat(llm): add citation grounding verifier [task 4.10] [R16.5]

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 9: Write the failing test for case and whitespace drift (Review Focus 1)**

Append to `tests/unit/test_citation_verification.py`:

```python
@pytest.mark.parametrize(
    "cited",
    ["doc-125-08", "DOC-125-08 ", "  Doc-125-08", "\tDOC-125-08\n"],
)
def test_case_and_whitespace_drift_still_matches(cited: str) -> None:
    """A model that lowercases or pads a citation has not hallucinated it.

    Treating these as mismatches would inflate the very rate the metric reports, so the
    comparison folds case and strips surrounding whitespace while the output keeps the id
    as the model wrote it, trimmed.
    """
    context = _context(_chunk("chunk-1", external_id="DOC-125-08"))

    verdict = verify_citations([cited], context)

    assert verdict.mismatch is False
    assert verdict.citations[0]["chunk_id"] == "chunk-1"
    assert verdict.citations[0]["citation_id"] == cited.strip()
```

- [ ] **Step 10: Run it**

Run: `uv run pytest tests/unit/test_citation_verification.py -k drift -p no:cacheprovider`
Expected: PASS, 4 passed. `_normalize` already handles this; the test pins it so a later "simplification" to exact matching fails loudly.

- [ ] **Step 11: Write the failing test for duplicate citations (Review Focus 2)**

Append to `tests/unit/test_citation_verification.py`:

```python
def test_duplicate_citations_count_once() -> None:
    """The same chunk cited repeatedly is one citation, so the denominator is not inflated."""
    context = _context(_chunk("chunk-1", external_id="DOC-125-08"))

    verdict = verify_citations(["DOC-125-08", "doc-125-08", "DOC-125-08"], context)

    assert verdict.cited_count == 1
    assert len(verdict.citations) == 1
    assert verdict.mismatch is False


def test_duplicate_mismatches_count_once() -> None:
    """A repeated invented id is reported once, not once per repetition."""
    context = _context(_chunk("chunk-1", external_id="DOC-125-08"))

    verdict = verify_citations(["DOC-999-99", "DOC-999-99"], context)

    assert verdict.mismatched == ["DOC-999-99"]
    assert verdict.cited_count == 1
    assert verdict.mismatch_ratio == 1.0
```

- [ ] **Step 12: Run it**

Run: `uv run pytest tests/unit/test_citation_verification.py -k duplicate -p no:cacheprovider`
Expected: PASS, 2 passed.

- [ ] **Step 13: Write the failing test for a chunk with no external_id (Review Focus 3)**

Append to `tests/unit/test_citation_verification.py`:

```python
def test_chunk_without_external_id_is_cited_by_chunk_id() -> None:
    """The template shows chunk_id when external_id is unset, so that id must match.

    `prompts/*.j2` renders `[CITATION: {{ chunk.external_id or chunk.chunk_id }}]`, so for a
    chunk with no external_id the chunk_id IS the citation id the model was given.
    """
    context = _context(_chunk("chunk-77", external_id=None))

    verdict = verify_citations(["chunk-77"], context)

    assert verdict.mismatch is False
    assert verdict.citations == [
        {
            "citation_id": "chunk-77",
            "chunk_id": "chunk-77",
            "document_id": "doc-kb-01",
            "external_id": None,
        }
    ]


def test_chunk_id_is_accepted_even_when_external_id_is_set() -> None:
    """Either alias of a supplied chunk is accepted (decision D1).

    A chunk_id names a chunk that genuinely was in the context, so it is not a hallucinated
    citation. R16.5 tests correspondence to supplied chunks, not alias discipline.
    """
    context = _context(_chunk("chunk-1", external_id="DOC-125-08"))

    verdict = verify_citations(["chunk-1"], context)

    assert verdict.mismatch is False
    assert verdict.citations[0]["external_id"] == "DOC-125-08"
```

- [ ] **Step 14: Run it**

Run: `uv run pytest tests/unit/test_citation_verification.py -k chunk_id -p no:cacheprovider`
Expected: PASS, 2 passed.

- [ ] **Step 15: Write the failing test for an empty context and for no citations (Review Focus 4)**

Append to `tests/unit/test_citation_verification.py`:

```python
def test_citations_with_no_supplied_chunks_are_all_mismatches() -> None:
    """Citing anything when nothing was retrieved is wholly ungrounded."""
    context = _context()

    verdict = verify_citations(["DOC-125-08"], context)

    assert verdict.supplied_count == 0
    assert verdict.mismatch is True
    assert verdict.mismatched == ["DOC-125-08"]
    assert verdict.mismatch_ratio == 1.0


def test_no_citations_is_not_a_mismatch() -> None:
    """A draft that cites nothing has made no false claim, whatever was supplied.

    Guards the zero denominator: mismatch_ratio must be 0.0, never a ZeroDivisionError.
    """
    context = _context(_chunk("chunk-1", external_id="DOC-125-08"))

    verdict = verify_citations([], context)

    assert verdict.mismatch is False
    assert verdict.cited_count == 0
    assert verdict.mismatch_ratio == 0.0
    assert verdict.citations == []


def test_blank_citation_strings_are_ignored() -> None:
    """An empty or whitespace-only entry is noise, not a hallucinated citation."""
    context = _context(_chunk("chunk-1", external_id="DOC-125-08"))

    verdict = verify_citations(["", "   ", "DOC-125-08"], context)

    assert verdict.mismatch is False
    assert verdict.cited_count == 1
```

- [ ] **Step 16: Run it**

Run: `uv run pytest tests/unit/test_citation_verification.py -p no:cacheprovider`
Expected: PASS, all tests in the file pass.

- [ ] **Step 17: Write the failing test proving a document_id does not match (Review Focus 5)**

Append to `tests/unit/test_citation_verification.py`:

```python
def test_document_id_is_not_an_accepted_citation() -> None:
    """Citing a document is not evidence a specific chunk was supplied (decision D2).

    Several chunks share one document_id, so accepting it would let a model name a document
    it saw one line of and have every claim about it counted as grounded.
    """
    context = _context(_chunk("chunk-1", external_id="DOC-125-08", document_id="doc-kb-01"))

    verdict = verify_citations(["doc-kb-01"], context)

    assert verdict.mismatch is True
    assert verdict.mismatched == ["doc-kb-01"]


def test_citation_index_holds_only_chunk_level_aliases() -> None:
    """build_citation_index exposes exactly the ids the prompt renders, and no others."""
    chunks = [
        _chunk("chunk-1", external_id="DOC-125-08", document_id="doc-kb-01"),
        _chunk("chunk-2", external_id=None, document_id="doc-kb-02"),
    ]

    index = build_citation_index(chunks)

    assert set(index) == {"doc-125-08", "chunk-1", "chunk-2"}
    assert "doc-kb-01" not in index
    assert "doc-kb-02" not in index
```

- [ ] **Step 18: Run it**

Run: `uv run pytest tests/unit/test_citation_verification.py -k "document_id or index" -p no:cacheprovider`
Expected: PASS, 2 passed.

- [ ] **Step 19: Verify the whole task, including coverage of the new module**

Run:
```bash
uv run pytest tests/unit/test_citation_verification.py -p no:cacheprovider
uv run pytest tests/unit --cov=packages.llm.citations --cov-report=term-missing -p no:cacheprovider
uv run ruff check packages/llm/citations.py tests/unit/test_citation_verification.py
uv run ruff format --check packages/llm/citations.py tests/unit/test_citation_verification.py
uv run mypy packages/
```
Expected: all tests pass; `packages/llm/citations.py` reports **100%** with no missing lines; ruff and format clean; mypy `Success`. If a line is uncovered, add the test that reaches it rather than lowering the bar.

- [ ] **Step 20: Commit**

```bash
git add tests/unit/test_citation_verification.py
git commit -m "test(llm): pin citation verifier edge cases [task 4.10] [R16.5]

Covers case and whitespace drift, duplicate citations, chunks without an
external_id, an empty context, blank entries, and the deliberate refusal to
accept a document_id as a chunk-level citation.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Citation metrics and the documented rate

**Files:**
- Modify: `packages/observability/metrics.py` (add two `Counter` fields to `PipelineMetrics`, register both in `create_pipeline_metrics`)
- Modify: `docs/observability.md` (add both rows to the §2.1 funnel table and the PromQL rate expression)
- Test: `tests/unit/test_observability_metrics.py` (append one test)

**Interfaces:**
- Consumes: the existing `PipelineMetrics` dataclass and `create_pipeline_metrics(registry)` factory, and `generate_metrics_payload(registry)` for assertions. Do not change their signatures.
- Produces:
  - `PipelineMetrics.citations_verified_total: Counter` with label `category`
  - `PipelineMetrics.citation_mismatches_total: Counter` with label `category`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_observability_metrics.py`:

```python
def test_citation_verification_metrics() -> None:
    """Verify the citation grounding counters are registered and emit (R16.5, R21.4)."""
    m = create_pipeline_metrics()

    assert m.citations_verified_total is not None
    assert m.citation_mismatches_total is not None

    m.citations_verified_total.labels(category="support").inc()
    m.citations_verified_total.labels(category="billing").inc()
    m.citation_mismatches_total.labels(category="support").inc()

    payload, _ = generate_metrics_payload(m.registry)
    payload_str = payload.decode("utf-8")

    assert 'citations_verified_total{category="support"} 1.0' in payload_str
    assert 'citations_verified_total{category="billing"} 1.0' in payload_str
    assert 'citation_mismatches_total{category="support"} 1.0' in payload_str
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/test_observability_metrics.py::test_citation_verification_metrics -p no:cacheprovider`
Expected: FAIL with `AttributeError: 'PipelineMetrics' object has no attribute 'citations_verified_total'`.

- [ ] **Step 3: Declare the two fields**

In `packages/observability/metrics.py`, in the `PipelineMetrics` counter field block, immediately after the existing `draft_validation_failures_total: Counter` line, add:

```python
    citations_verified_total: Counter
    citation_mismatches_total: Counter
```

- [ ] **Step 4: Register the two counters**

In `create_pipeline_metrics`, immediately after the existing `draft_validation_failures_total=Counter(...)` block, add:

```python
        citations_verified_total=Counter(
            "citations_verified_total",
            "Generated drafts whose citations were checked against the supplied context (R16.5)",
            ["category"],
            registry=reg,
        ),
        citation_mismatches_total=Counter(
            "citation_mismatches_total",
            "Generated drafts citing at least one chunk absent from the context (R16.5)",
            ["category"],
            registry=reg,
        ),
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/unit/test_observability_metrics.py -p no:cacheprovider`
Expected: PASS, all tests in the file pass.

- [ ] **Step 6: Document both metrics and the rate expression**

In `docs/observability.md`, add these two rows to the end of the §2.1 *Funnel Accounting Counters* table:

```markdown
| `citations_verified_total` | Counter | `category` | Drafts whose citations were checked against the supplied context (R16.5). |
| `citation_mismatches_total` | Counter | `category` | Drafts citing at least one chunk absent from the context (R16.5). |
```

Then add this subsection immediately after that table:

````markdown
#### Hallucinated-citation rate (R16.5)

`citation_mismatch` is exported as two monotonic counters rather than a gauge, so the rate is
computed at query time and no worker has to hold accumulator state:

```promql
sum(rate(citation_mismatches_total[5m])) / sum(rate(citations_verified_total[5m]))
```

Per category, for the retrieval-quality dashboard panel (task 7.4):

```promql
sum by (category) (rate(citation_mismatches_total[5m]))
  / sum by (category) (rate(citations_verified_total[5m]))
```

A draft is counted in `citations_verified_total` whenever a schema-valid draft was produced,
including one that cited nothing — so the denominator is "drafts that could have cited" and the
ratio is the share of drafts containing at least one ungrounded citation.
````

- [ ] **Step 7: Verify the task**

Run:
```bash
uv run pytest tests/unit/test_observability_metrics.py -p no:cacheprovider
uv run ruff check packages/observability/metrics.py tests/unit/test_observability_metrics.py
uv run ruff format --check packages/observability/metrics.py tests/unit/test_observability_metrics.py
uv run mypy packages/
```
Expected: tests pass, ruff clean, format clean, mypy `Success`.

- [ ] **Step 8: Commit**

```bash
git add packages/observability/metrics.py tests/unit/test_observability_metrics.py docs/observability.md
git commit -m "feat(observability): add citation verification counters and rate query [task 4.10] [R16.5, R21.4]

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Wire verification into generation

**Files:**
- Modify: `packages/llm/generator.py` (add two `GenerationResult` fields and a property; call the verifier in `generate_draft` after validation; add one private recorder)
- Modify: `specs/tasks.md` (task 4.10 entry)
- Test: `tests/unit/test_citation_verification_generation.py`

**Interfaces:**
- Consumes: `verify_citations(cited_ids, context) -> CitationVerdict` and `CitationVerdict` from Task 1; `PipelineMetrics.citations_verified_total` and `.citation_mismatches_total` from Task 2; the existing `DraftReplyPayload.knowledge_chunks: list[str]` from `packages/llm/validation.py`; and the existing `GenerationResult` dataclass.
- Produces:
  - `GenerationResult.citation_verdict: CitationVerdict | None`
  - `GenerationResult.citation_mismatch: bool` (a property delegating to the verdict, `False` when there is no verdict)
  - **For task 4.11 (draft persistence):** persist `result.citation_verdict.citations` into `generated_draft.citations` and `result.citation_mismatch` into `generated_draft.citation_mismatch`. Do **not** persist `result.content["knowledge_chunks"]` — it still holds the rejected ids by design (D5).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_citation_verification_generation.py`:

```python
"""Integration of citation verification into draft generation (R16.5, R21.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from packages.domain.entities import (
    Candidate,
    ContextPackage,
    EmailAddress,
    NormalizedMessage,
)
from packages.llm import (
    AgentProfileRegistry,
    FakeLLMProvider,
    SinglePassGenerator,
)
from packages.observability.metrics import (
    create_pipeline_metrics,
    generate_metrics_payload,
)


def _context(*chunks: Candidate) -> ContextPackage:
    message = NormalizedMessage(
        message_id=uuid4(),
        organization_id=uuid4(),
        mailbox_id=uuid4(),
        thread_id=uuid4(),
        provider="mock",
        provider_message_id="msg-cite-gen-001",
        sender=EmailAddress(email="customer@example.com", name="Alice Customer"),
        subject="How do I reset my password?",
        subject_normalized="How do I reset my password?",
        body_text="I forgot my password.",
        body_text_clean="I forgot my password.",
        received_at=datetime.now(UTC),
    )
    return ContextPackage(
        agent_instructions="You are an enterprise AI assistant.",
        category_instructions="Address technical support questions.",
        current_message=message,
        retrieved_chunks=list(chunks),
    )


def _supplied_chunk() -> Candidate:
    return Candidate(
        chunk_id="chunk-1",
        document_id="doc-kb-01",
        content="To reset a password, open settings and choose Reset Password.",
        external_id="DOC-125-08",
    )


def _reply(*cited: str) -> dict[str, Any]:
    return {
        "action": "reply",
        "draft": "Hello Alice, open settings and choose Reset Password.",
        "confidence": 0.95,
        "knowledge_chunks": list(cited),
        "thread_summary_updated": False,
        "model_tier": "routine",
    }


@pytest.fixture
def profile_registry() -> AgentProfileRegistry:
    return AgentProfileRegistry.from_yaml("config/agent_profiles.yaml")


def _metrics_payload(metrics: Any) -> str:
    payload, _ = generate_metrics_payload(metrics.registry)
    return str(payload.decode("utf-8"))


@pytest.mark.asyncio
async def test_grounded_draft_reports_no_citation_mismatch(
    profile_registry: AgentProfileRegistry,
) -> None:
    """A draft citing a supplied chunk is clean and counted in the denominator (R16.5)."""
    metrics = create_pipeline_metrics()
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(default_response=_reply("DOC-125-08")),
        profile_registry=profile_registry,
        metrics=metrics,
    )

    result = await generator.generate_draft(
        _context(_supplied_chunk()), category="support"
    )

    assert result.citation_mismatch is False
    assert result.citation_verdict is not None
    assert result.citation_verdict.citations[0]["chunk_id"] == "chunk-1"

    payload = _metrics_payload(metrics)
    assert 'citations_verified_total{category="support"} 1.0' in payload
    assert 'citation_mismatches_total{category="support"}' not in payload
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/test_citation_verification_generation.py -p no:cacheprovider`
Expected: FAIL with `AttributeError: 'GenerationResult' object has no attribute 'citation_mismatch'`.

- [ ] **Step 3: Add the imports and the result fields**

In `packages/llm/generator.py`, add to the imports (after the `from packages.llm.budget import (...)` block):

```python
from packages.llm.citations import CitationVerdict, verify_citations
```

Then in the `GenerationResult` dataclass, after the existing `validated_payload: DraftReplyPayload | None = None` line, add:

```python
    citation_verdict: CitationVerdict | None = None

    @property
    def citation_mismatch(self) -> bool:
        """True when the draft cited a chunk absent from its context (R16.5)."""
        return self.citation_verdict is not None and self.citation_verdict.mismatch
```

- [ ] **Step 4: Add the metric recorder**

In `packages/llm/generator.py`, add this method beside the existing `_record_repair_outcome`:

```python
    def _record_citation_verdict(self, verdict: CitationVerdict, category: str) -> None:
        """Count the grounding check for this draft (R16.5, R21.4).

        Every schema-valid draft increments the verified counter, including one that cited
        nothing, so the denominator is "drafts that could have cited" and the exported ratio
        is the share of drafts carrying at least one ungrounded citation.
        """
        metrics = self.metrics
        if metrics is None:
            return
        if hasattr(metrics, "citations_verified_total"):
            metrics.citations_verified_total.labels(category=category).inc()
        if verdict.mismatch and hasattr(metrics, "citation_mismatches_total"):
            metrics.citation_mismatches_total.labels(category=category).inc()
```

- [ ] **Step 5: Verify citations in `generate_draft`**

In `packages/llm/generator.py`, immediately after the `try: ... finally: self._emit_generation_metrics(...)` block and before the `total_input, total_output, total_latency = self._totals(attempts)` line, insert:

```python
        # Verify citation grounding (R16.5). A mismatch flags the draft; it never fails the
        # job, which is R16.3's job. The label uses the resolved profile when no category was
        # supplied, so it stays bounded by config/agent_profiles.yaml either way.
        citation_verdict = verify_citations(validated_payload.knowledge_chunks, context)
        if citation_verdict.mismatch:
            logger.warning(
                "draft_citation_mismatch",
                extra={
                    "job_id": tracker.job_id,
                    "mismatched_citations": citation_verdict.mismatched,
                    "supplied_chunks": citation_verdict.supplied_count,
                },
            )
        self._record_citation_verdict(
            citation_verdict,
            category=category or resolved_profile.profile,
        )
```

Then add `citation_verdict=citation_verdict,` to the `GenerationResult(...)` constructor call, after the existing `validated_payload=validated_payload,` argument.

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/unit/test_citation_verification_generation.py -p no:cacheprovider`
Expected: PASS, 1 passed.

- [ ] **Step 7: Write the failing test for a hallucinated citation in the real path**

Append to `tests/unit/test_citation_verification_generation.py`:

```python
@pytest.mark.asyncio
async def test_hallucinated_citation_flags_the_draft_without_failing_the_job(
    profile_registry: AgentProfileRegistry,
) -> None:
    """An ungrounded citation is rejected and flagged, and the draft is still returned.

    R16.5 records a flag; failing the job is R16.3's behaviour for a schema failure. A
    reviewer needs to see the draft in order to judge it, so it must survive.
    """
    metrics = create_pipeline_metrics()
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(
            default_response=_reply("DOC-125-08", "DOC-999-99")
        ),
        profile_registry=profile_registry,
        metrics=metrics,
    )

    result = await generator.generate_draft(
        _context(_supplied_chunk()), category="support"
    )

    assert result.citation_mismatch is True
    assert result.citation_verdict is not None
    assert result.citation_verdict.mismatched == ["DOC-999-99"]
    # The accepted citation survives; only the invented one is rejected.
    assert [c["external_id"] for c in result.citation_verdict.citations] == ["DOC-125-08"]
    # The draft itself is intact and the model's own claim is preserved for debugging (D5).
    assert result.content["draft"].startswith("Hello Alice")
    assert result.content["knowledge_chunks"] == ["DOC-125-08", "DOC-999-99"]

    payload = _metrics_payload(metrics)
    assert 'citations_verified_total{category="support"} 1.0' in payload
    assert 'citation_mismatches_total{category="support"} 1.0' in payload
```

- [ ] **Step 8: Run it**

Run: `uv run pytest tests/unit/test_citation_verification_generation.py -p no:cacheprovider`
Expected: PASS, 2 passed.

- [ ] **Step 9: Write the failing test for the repaired-draft path**

Append to `tests/unit/test_citation_verification_generation.py`:

```python
@pytest.mark.asyncio
async def test_citations_of_a_repaired_draft_are_verified(
    profile_registry: AgentProfileRegistry,
) -> None:
    """Verification runs on the payload that survived repair, not the rejected one.

    The repair retry replaces the draft, so a first attempt that cited nothing must not
    decide the verdict for a second attempt that cited something ungrounded.
    """
    metrics = create_pipeline_metrics()
    malformed = {"action": "reply", "draft": "x", "confidence": 0.9, "knowledge_chunks": []}
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(
            canned_responses=[malformed, _reply("DOC-999-99")]
        ),
        profile_registry=profile_registry,
        metrics=metrics,
    )

    result = await generator.generate_draft(
        _context(_supplied_chunk()), category="support"
    )

    assert result.is_repaired is True
    assert result.citation_mismatch is True
    assert result.citation_verdict is not None
    assert result.citation_verdict.mismatched == ["DOC-999-99"]
    assert 'citation_mismatches_total{category="support"} 1.0' in _metrics_payload(metrics)
```

- [ ] **Step 10: Run it**

Run: `uv run pytest tests/unit/test_citation_verification_generation.py -p no:cacheprovider`
Expected: PASS, 3 passed.

- [ ] **Step 11: Write the failing test for an empty retrieval context (Review Focus 4)**

Append to `tests/unit/test_citation_verification_generation.py`:

```python
@pytest.mark.asyncio
async def test_draft_citing_nothing_is_counted_but_not_flagged(
    profile_registry: AgentProfileRegistry,
) -> None:
    """A draft that cites nothing is clean, and still counts toward the denominator."""
    metrics = create_pipeline_metrics()
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(default_response=_reply()),
        profile_registry=profile_registry,
        metrics=metrics,
    )

    result = await generator.generate_draft(
        _context(_supplied_chunk()), category="support"
    )

    assert result.citation_mismatch is False
    assert result.citation_verdict is not None
    assert result.citation_verdict.cited_count == 0
    assert result.citation_verdict.mismatch_ratio == 0.0

    payload = _metrics_payload(metrics)
    assert 'citations_verified_total{category="support"} 1.0' in payload
    assert 'citation_mismatches_total{category="support"}' not in payload


@pytest.mark.asyncio
async def test_citing_when_no_chunks_were_retrieved_is_wholly_ungrounded(
    profile_registry: AgentProfileRegistry,
) -> None:
    """With an empty retrieval context every citation is a hallucination."""
    metrics = create_pipeline_metrics()
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(default_response=_reply("DOC-125-08")),
        profile_registry=profile_registry,
        metrics=metrics,
    )

    result = await generator.generate_draft(_context(), category="support")

    assert result.citation_mismatch is True
    assert result.citation_verdict is not None
    assert result.citation_verdict.supplied_count == 0
    assert result.citation_verdict.mismatch_ratio == 1.0
    assert 'citation_mismatches_total{category="support"} 1.0' in _metrics_payload(metrics)


@pytest.mark.asyncio
async def test_verification_runs_without_metrics_configured(
    profile_registry: AgentProfileRegistry,
) -> None:
    """A generator built without metrics still produces a verdict, and does not crash."""
    generator = SinglePassGenerator(
        llm_provider=FakeLLMProvider(default_response=_reply("DOC-999-99")),
        profile_registry=profile_registry,
    )

    result = await generator.generate_draft(
        _context(_supplied_chunk()), category="support"
    )

    assert result.citation_mismatch is True
```

- [ ] **Step 12: Run it**

Run: `uv run pytest tests/unit/test_citation_verification_generation.py -p no:cacheprovider`
Expected: PASS, 6 passed.

- [ ] **Step 13: Run the full suite for regressions**

Run:
```bash
uv run pytest tests/unit -p no:cacheprovider
uv run pytest tests/integration -p no:cacheprovider
```
Expected: both green, and **more unit tests than before** this task. Any pre-existing test that now fails is a real signal about the verifier's identifier rules (decision D1) — investigate it rather than editing the assertion. In particular the `canned_reply` fixtures in `tests/unit/test_single_pass_generator.py` and `tests/unit/test_complexity_router_integration.py` cite `chunk-1` while their Candidate's `external_id` is `KB-PWD-01`; D1 accepts that, so they must still pass. If they do not, D1 was implemented as external-id-only and needs fixing.

- [ ] **Step 14: Verify lint, types, and coverage**

Run:
```bash
uv run ruff check packages/ tests/
uv run ruff format --check packages/llm/ tests/unit/test_citation_verification.py tests/unit/test_citation_verification_generation.py
uv run mypy packages/
uv run pytest tests/unit --cov=packages.llm.citations --cov=packages.llm.generator --cov-report=term-missing -p no:cacheprovider
```
Expected: ruff clean, format clean, mypy `Success`, and no uncovered lines in `packages/llm/citations.py`.

- [ ] **Step 15: Check the stack still boots (Definition of Done §3.3)**

Run:
```bash
uv run python -c "import services.api.main; print('api imports OK')"
docker compose ps --format '{{.Name}}\t{{.Status}}'
```
Expected: the import succeeds and no container has left a healthy state. This task touches only `packages/llm` and `packages/observability`, so a rebuild is not required.

- [ ] **Step 16: Update the spec's work queue**

In `specs/tasks.md`, replace the task 4.10 entry with:

```markdown
- [x] **4.10 Citation verification**
  - Reject citations naming chunks that were not supplied in the context; set `citation_mismatch` and export its rate as a metric.
  - Done: `packages/llm/citations.py` verifies every cited id against the context's chunk-level aliases (`external_id`, falling back to `chunk_id` as `prompts/*.j2` renders them); `SinglePassGenerator` attaches a `CitationVerdict` to `GenerationResult` and flags `citation_mismatch` without failing the job. `citations_verified_total{category}` and `citation_mismatches_total{category}` are exported, with the rate's PromQL documented in `docs/observability.md`.
  - Note: the flag is produced but not yet persisted — `generated_draft.citation_mismatch` and `.citations` are written by task 4.11, which must persist `verdict.citations` rather than `content["knowledge_chunks"]`. The Grafana panel for the rate belongs to task 7.4.
  - _Requirements: R16.5_
```

- [ ] **Step 17: Commit**

```bash
git add packages/llm/generator.py tests/unit/test_citation_verification_generation.py specs/tasks.md
git commit -m "feat(llm): verify citation grounding during generation [task 4.10] [R16.5, R21.4]

Run the citation verifier on the validated payload and attach the verdict to
GenerationResult. An ungrounded citation is rejected from the accepted set and
flags the draft; it never fails the job, which is R16.3's behaviour. The
model's own claim stays in content for debugging, so task 4.11 must persist
verdict.citations rather than content['knowledge_chunks'].

Exports citations_verified_total and citation_mismatches_total by category.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage.**

| Spec item | Where |
|---|---|
| R16.5 "reject citations that do not correspond to chunks actually supplied" | Task 1 Step 3 (`verify_citations` partitions; `citations` holds accepted only) |
| R16.5 "recording a `citation_mismatch` flag" | Task 3 Steps 3, 5 (`GenerationResult.citation_mismatch`); persistence is 4.11 per D5 |
| tasks.md 4.10 "export its rate as a metric" | Task 2 (two counters + documented PromQL, per D4) |
| design.md:618 "hallucinated-citation detector, its rate is a reportable quality metric" | Task 2 Step 6 |
| design.md:370 / proposal §20 "each with citation id" | D1; Task 1 Step 13 pins the template's own fallback rule |
| proposal §24 response schema `knowledge_chunks` | Task 3 consumes `DraftReplyPayload.knowledge_chunks` (already enforced by task 4.9) |
| R21.4 metric conventions, R21.5 histograms | Task 2 uses Counters; no new latency metric, so R21.5 does not apply |
| CLAUDE.md §3.4 config keys documented | No config keys added — stated in Global Constraints |

Gaps found and accepted: `generated_draft.citations` / `.citation_mismatch` are written by task 4.11, not here; the Grafana panel is task 7.4. Both are recorded in the `specs/tasks.md` note in Task 3 Step 16 so the Phase 4 gate does not assume them.

**2. Placeholder scan.** No TBD/TODO, no "add error handling", no "similar to Task N". Every code step carries the actual code, and every command step an explicit expected result.

**3. Type consistency.** `CitationVerdict`, `build_citation_index` and `verify_citations` are named identically in Task 1's definition, Task 1's re-export, and Task 3's consumption. `verify_citations(cited_ids, context)` is called positionally in Task 3 exactly as defined in Task 1. The four `citations` dict keys in Task 1 Step 3 are the four asserted in Steps 1, 13 and in Task 3 Steps 1, 7. The metric attribute names `citations_verified_total` / `citation_mismatches_total` are identical in Task 2 Steps 1, 3, 4 and Task 3 Step 4.

**4. Review Focus.** All five lines have a pinning test: case/whitespace (T1 S9), duplicates (T1 S11), missing `external_id` (T1 S13), empty context and zero denominator (T1 S15 + T3 S11), `document_id` rejected (T1 S17). Task 3 Step 13 additionally guards the regression D1 is designed to avoid.

---

## Skills loaded for this plan

- `superpowers:writing-plans` — this document.
- `.agent/skills/pytest-coverage/SKILL.md` — adopted: coverage is verified in Task 1 Step 19 and Task 3 Step 14, with 100% required on the new module.
- `fullstack-dev-skills:rag-architect` — read; its subject is vector stores, chunking and retrieval pipelines, so only its "measure retrieval quality, don't infer it from LLM output quality" principle applies, which is why the mismatch rate is exported as counters rather than inferred.
- `.agent/skills/rag-eval/SKILL.md` — read and **rejected**: it drives NVIDIA RAG Blueprint filesystem benchmarks (`scripts/eval/evaluate_rag.py`, RAGAS, `NVIDIA_API_KEY`) and its own "When not to use" excludes repos without that layout and production monitoring. Neither applies to this task.
- `fullstack-dev-skills:sql-pro`, `postgres-pro`, `database-optimizer` — loaded earlier at the user's request; this task adds no SQL, no schema change and no query, so none applies. They become relevant at task 4.11.
