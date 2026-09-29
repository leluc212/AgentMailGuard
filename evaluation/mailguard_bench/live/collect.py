"""Wait for one case's job to finish, then read what the services persisted (task 7.20; R21).

    FedCase ─▶ find_job          processing_job, by the key the mail-connector derived for it
            ─▶ wait_for_job      until COMPLETED · DRAFTED · FAILED · DEAD_LETTER
            ─▶ processing_event  gate outcome · stage timings · the ai-worker's context_built
            ─▶ classification_result   the live triage decision
            ─▶ generated_draft   what the drafting consumer persisted (never approved or sent)
            ─▶ guard audit line  guarded configs only: report, guard LLM calls, timings
            ─▶ ``result`` of a ``mailguard-bench-result.v3`` row

The row keeps the v1 blocks (``host``, ``generation``, ``draft``, ``final_draft``,
``timings_ms``, ``guard_llm``, ``report`` ...) so v1's scoring flatten reads it unchanged, and
adds the flat ``final_body`` / ``final_action`` / ``retrieved`` names of ``scoring.RawRecord``
and ``result.pipeline`` (transport, job state, triage, whether drafting was reached, the
context flags, stage timings).

The persisted draft is the authority for ``final_body`` and ``final_action``: in a v1 row the
key ``final_action`` was the guard's decision ("allow"), in a draft it is the reply action
("reply"), and the draft is what a reviewer would have seen. The guard audit line supplies
everything the draft does not hold.

Nothing here writes: the collector only reads rows the services own.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from evaluation.mailguard_bench.case_adapter import EvalCase
from evaluation.mailguard_bench.guard_build import NATIVE_CONFIG
from evaluation.mailguard_bench.live.feeder import Deadline, FedCase, Sleep
from packages.context.builder import DefaultInstructionProvider
from packages.core.idempotency import derive_idempotency_key
from packages.db.classification import ClassificationResultRow, ClassificationStore
from packages.db.draft import DraftStore
from packages.db.job import JobStore
from packages.domain.entities import GeneratedDraft, Job, ProcessingEvent
from packages.domain.state_machine import JobState

TRANSPORT = "services-v2"
TERMINAL_STATES = frozenset(
    state.value
    for state in (JobState.COMPLETED, JobState.DRAFTED, JobState.FAILED, JobState.DEAD_LETTER)
)
FAILED_STATES = frozenset({JobState.FAILED.value, JobState.DEAD_LETTER.value})
NORMALIZE_OPERATION = "normalize"
"""The operation the mail-connector derives the hand-off's idempotency key for."""
STATE_TRANSITION_EVENT = "state_transition"
CONTEXT_BUILT_EVENT = "context_built"
"""The event the ai-worker inserts after building the context (task 7.20, package A.7)."""
# What the early-exit gate decided; the same values as services.triage_worker.GateAction
# (a test keeps them equal). Importing that enum would load the whole triage package,
# scikit-learn included, into a runner that only reads its result.
GATE_EARLY_EXIT = "early_exit"
GATE_TEMPLATE = "template_reply"
GATE_RAG = "proceed_rag"
GATE_NO_RAG = "proceed_no_rag"
LAST_ERROR_CHARS = 200
"""How much of a job's last error a timeout message carries."""
AUDIT_GRACE_S = 10.0
"""How long a drafted job may wait for its audit line: the guard-worker writes the line
around the DRAFTED commit, not inside it."""
NATIVE_PROMPT_MODE = "native"
_AUDIT_ID_KEYS = frozenset({"message_id", "organization_id", "config"})


class PipelineJobError(RuntimeError):
    """The case's job is missing, vanished or ended FAILED or DEAD_LETTER."""


class AuditMissingError(RuntimeError):
    """A guarded case was drafted, but the guard-worker wrote no audit line for it."""


class ContextEventMissingError(RuntimeError):
    """A job reached CONTEXT_READY without the ai-worker's ``context_built`` event."""


@dataclass(frozen=True)
class PipelineStores:
    """The three stores the collector reads; each query carries the organization id."""

    jobs: JobStore
    classifications: ClassificationStore
    drafts: DraftStore


async def find_job(jobs: JobStore, fed: FedCase) -> Job:
    """The case's ``processing_job``, located by the key the mail-connector derived for it.

    The orchestrator creates the job before it publishes and does not return its id, so the
    id is found again through the idempotency key, computed by the same function.

    Raises:
        PipelineJobError: If the hand-off left no job in the organization.
    """
    key = derive_idempotency_key(
        fed.organization_id, fed.mailbox_id, fed.provider_message_id, NORMALIZE_OPERATION
    )
    job = await jobs.get_job_by_idempotency_key(fed.organization_id, key)
    if job is None:
        raise PipelineJobError(
            f"no processing_job for {fed.provider_message_id} in organization "
            f"{fed.organization_id} after the hand-off"
        )
    return job


async def wait_for_job(
    jobs: JobStore, job: Job, deadline: Deadline, *, sleep: Sleep, poll_interval_s: float
) -> Job:
    """Poll until the job is COMPLETED, DRAFTED, FAILED or DEAD_LETTER; return it.

    Raises:
        CaseTimeoutError: If the case budget is spent first; the message names the state the
            job was in, which says which stage stalled, and its last error. A failing model
            call does not fail the job: the retry ladder holds it (30 s, 5 m and 30 m tiers),
            so an unreachable model or an HTTP 429 shows up here as a stalled state.
        PipelineJobError: If the job row disappears (its organization was deleted).
    """
    while True:
        current = await jobs.get_job(job.organization_id, job.id)
        if current is None:
            raise PipelineJobError(f"job {job.id} disappeared while it was awaited")
        if current.state in TERMINAL_STATES:
            return current
        detail = f"state {current.state}"
        if current.last_error:
            detail += f"; last error: {current.last_error[:LAST_ERROR_CHARS]}"
        deadline.check(f"waiting for job {job.id} ({detail})")
        await sleep(min(poll_interval_s, deadline.remaining()))


def _transitions(events: list[ProcessingEvent]) -> list[ProcessingEvent]:
    """The state transitions among a job's events (diagnostic events are not transitions)."""
    return [event for event in events if event.event_type == STATE_TRANSITION_EVENT]


def _entered(
    moves: list[ProcessingEvent], state: JobState, *, from_state: JobState | None = None
) -> datetime | None:
    for event in moves:
        if event.state_to == state.value and (
            from_state is None or event.state_from == from_state.value
        ):
            return event.created_at
    return None


def _left(moves: list[ProcessingEvent], state: JobState) -> datetime | None:
    for event in moves:
        if event.state_from == state.value:
            return event.created_at
    return None


def _ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def stage_timings(events: list[ProcessingEvent]) -> dict[str, int | None]:
    """Milliseconds between the job's state transitions; None for a stage it never reached.

    ``context`` runs from QUEUED to CONTEXT_READY (the lane wait plus context building, since
    the worker does not record the two apart) and ``generation`` from GENERATING to DRAFTED
    (the drafting call plus persisting the draft).
    """
    moves = _transitions(events)
    received = _entered(moves, JobState.RECEIVED)
    normalized = _entered(moves, JobState.NORMALIZED)
    triaged = _left(moves, JobState.CLASSIFIED)
    queued = _entered(moves, JobState.QUEUED)
    context_ready = _entered(moves, JobState.CONTEXT_READY)
    generating = _entered(moves, JobState.GENERATING)
    drafted = _entered(moves, JobState.DRAFTED, from_state=JobState.GENERATING)
    last = max((event.created_at for event in moves), default=None)
    return {
        "normalize": _ms(received, normalized),
        "triage": _ms(normalized, triaged),
        "context": _ms(queued, context_ready),
        "drafting": _ms(context_ready, drafted),
        "generation": _ms(generating, drafted),
        "total": _ms(received, last),
    }


def gate_outcome(events: list[ProcessingEvent]) -> str | None:
    """What the early-exit gate decided, read from the transition it committed.

    The gate leaves CLASSIFIED for COMPLETED (early exit), DRAFTED (template reply) or
    QUEUED (AI drafting, with or without retrieval) and records no outcome of its own.
    None when the job never got past triage.
    """
    for event in _transitions(events):
        if event.state_from != JobState.CLASSIFIED.value:
            continue
        payload = event.payload or {}
        if event.state_to == JobState.COMPLETED.value and payload.get("early_exit"):
            return GATE_EARLY_EXIT
        if event.state_to == JobState.DRAFTED.value and payload.get("template_reply"):
            return GATE_TEMPLATE
        if event.state_to == JobState.QUEUED.value:
            return GATE_RAG if payload.get("retrieval_required") else GATE_NO_RAG
    return None


def read_audit_line(
    path: Path, *, organization_id: UUID, message_id: UUID | str, config: str
) -> dict[str, Any] | None:
    """The guard-worker's audit line of one job; the last one wins (a case may be retried).

    A line still being written, another job's line and the guard's own L5 records (which
    carry neither id) are skipped. The file is shared by the guard-worker and read while it
    grows, so nothing here assumes it is complete.
    """
    if not path.exists():
        return None
    org, message = str(organization_id), str(message_id)
    found: dict[str, Any] | None = None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if org not in line or message not in line:  # cheap test before parsing
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(row, dict)
                and str(row.get("organization_id")) == org
                and str(row.get("message_id")) == message
                and row.get("config", config) == config
            ):
                found = row
    return found


async def wait_for_audit(
    path: Path,
    *,
    organization_id: UUID,
    message_id: UUID | str,
    config: str,
    grace_s: float,
    sleep: Sleep,
    poll_interval_s: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any] | None:
    """The audit line of a drafted job, waiting up to ``grace_s`` for it to be written."""
    grace = Deadline(grace_s, monotonic=monotonic)
    while True:
        row = read_audit_line(
            path, organization_id=organization_id, message_id=message_id, config=config
        )
        if row is not None:
            return row
        if grace.remaining() <= 0:
            return None
        await sleep(min(poll_interval_s, grace.remaining()))


def _audit_result(row: Mapping[str, Any]) -> dict[str, Any]:
    """The result fields of an audit line: under ``result``, or beside the ids."""
    nested = row.get("result")
    if isinstance(nested, Mapping):
        return dict(nested)
    return {key: value for key, value in row.items() if key not in _AUDIT_ID_KEYS}


def _generation_block(
    draft: GeneratedDraft | None, *, called: bool, latency_ms: int | None
) -> dict[str, Any]:
    """The row's ``generation`` block, as the persisted draft can describe it.

    The draft does not hold the agent profile, the repair count or the raw reply, so
    ``profile`` and ``repair_attempts`` are None and ``reply_v1`` is rebuilt from the fields
    scoring reads (``draft``, ``action``). ``calls`` is the design's one call per job.
    """
    if draft is None:
        model, prompt_version, input_tokens, output_tokens, mismatch = None, None, 0, 0, False
    else:
        model, prompt_version = draft.model_name, draft.prompt_version
        input_tokens, output_tokens = draft.input_tokens, draft.output_tokens
        mismatch = draft.citation_mismatch
    reply_v1 = (
        {
            "draft": draft.body,
            "action": draft.action,
            "confidence": draft.confidence,
            "citations": list(draft.citations),
        }
        if draft is not None and called
        else None
    )
    return {
        "called": called,
        "model": model,
        "profile": None,
        "prompt_version": prompt_version,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": (latency_ms or 0) if called else 0,
        "repair_attempts": None if called else 0,
        "calls": 1 if called else 0,
        "citation_mismatch": mismatch,
        "reply_v1": reply_v1,
    }


def _draft_blocks(
    draft: GeneratedDraft | None, *, original: str | None = None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The v1 ``draft`` and ``final_draft`` blocks; no draft is an empty body and action none."""
    if draft is None:
        return {
            "action": "none",
            "body_original": "",
            "body_after_guard": "",
            "redacted_by_l4": False,
        }, None
    body_original = draft.body if original is None else original
    block = {
        "action": draft.action,
        "body_original": body_original,
        "body_after_guard": draft.body,
        "redacted_by_l4": body_original != draft.body,
    }
    return block, {"action": draft.action, "body": draft.body}


def _unguarded_record(system_instructions: str) -> dict[str, Any]:
    """The v1 guard fields of a row no guard touched: nothing blocked, nothing flagged."""
    return {
        "blocked_inbound": False,
        "blocked_outbound": False,
        "blocked": False,
        "inbound_action": None,
        "rule": None,
        "decision_stage": None,
        "layers_flagged": [],
        "threat_types": [],
        "detected_layers": [],
        "l3b_quarantined": [],
        "max_severity": None,
        "prompt_mode": None,
        "guard_llm": {"model": "", "calls": 0, "input_tokens": 0, "output_tokens": 0},
        "job_result": None,
        "report": None,
        "guard_errors": [],
        "system_instructions": system_instructions,
    }


def _retrieved(fed: FedCase, context: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The chunks the ai-worker retrieved, mapped back to the case KB documents and their poison.

    Retrieval reports each chunk with the document it belongs to; the document ids are the
    ones the API returned when the feeder uploaded the case KB.
    """
    by_document = {str(doc.document_id): doc for doc in fed.docs}
    entries: list[dict[str, Any]] = []
    for entry in context.get("retrieved") or []:
        document_id = str(entry.get("document_id"))
        doc = by_document.get(document_id)
        entries.append(
            {
                "rank": entry.get("rank"),
                "rag_chunk_id": str(entry.get("chunk_id")),
                "rag_document_id": document_id,
                "case_chunk_id": doc.case_chunk_id if doc is not None else None,
                "poisoned": bool(doc is not None and doc.poisoned),
                "rerank_score": entry.get("rerank_score"),
            }
        )
    return entries


def _triage_block(
    classification: ClassificationResultRow | None, gate: str | None
) -> dict[str, Any]:
    """The live triage decision; every field is None when no classification was persisted."""
    return {
        "decided_by": classification.decided_by if classification else None,
        "category": classification.category if classification else None,
        "intent": classification.intent if classification else None,
        "priority": classification.priority if classification else None,
        "reply_required": classification.reply_required if classification else None,
        "retrieval_required": classification.retrieval_required if classification else None,
        "model_name": classification.model_name if classification else None,
        "latency_ms": classification.latency_ms if classification else None,
        "gate_outcome": gate,
    }


class LiveCollector:
    """Turns one fed case into the ``result`` of its row."""

    def __init__(
        self,
        *,
        stores: PipelineStores,
        config: str,
        audit_path: Path | None,
        poll_interval_s: float = 1.0,
        audit_grace_s: float = AUDIT_GRACE_S,
        sleep: Sleep = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if config != NATIVE_CONFIG and audit_path is None:
            raise ValueError(f"{config} is drafted by the guard-worker: pass its audit log path")
        self.stores = stores
        self.config = config
        self.audit_path = audit_path
        self.poll_interval_s = poll_interval_s
        self.audit_grace_s = audit_grace_s
        self.sleep = sleep
        self.monotonic = monotonic

    async def collect(self, case: EvalCase, fed: FedCase, deadline: Deadline) -> dict[str, Any]:
        """Wait for the job, then build the row's ``result`` from what the services wrote.

        Raises:
            CaseTimeoutError: If the job is not terminal in time.
            PipelineJobError: If the job failed.
            ContextEventMissingError: If a job reached CONTEXT_READY with no ``context_built``.
            AuditMissingError: If a guarded case was drafted and has no audit line.
        """
        jobs = self.stores.jobs
        job = await find_job(jobs, fed)
        job = await wait_for_job(
            jobs, job, deadline, sleep=self.sleep, poll_interval_s=self.poll_interval_s
        )
        if job.state in FAILED_STATES:
            raise PipelineJobError(
                f"case {case.case_id}: job {job.id} ended {job.state}: "
                f"{job.last_error or 'no error recorded'}"
            )
        events = await jobs.list_events_for_job(job.organization_id, job.id)
        moves = _transitions(events)
        context = next(
            (e.payload or {} for e in events if e.event_type == CONTEXT_BUILT_EVENT), None
        )
        if context is None and any(e.state_to == JobState.CONTEXT_READY.value for e in moves):
            raise ContextEventMissingError(
                f"case {case.case_id}: job {job.id} reached CONTEXT_READY but the ai-worker "
                f"left no {CONTEXT_BUILT_EVENT!r} event (task 7.20, package A.7)"
            )
        reached_drafting = any(e.state_to == JobState.GENERATING.value for e in moves)
        classification = (
            await self.stores.classifications.get_latest_classification_by_message(
                job.organization_id, job.message_id
            )
            if job.message_id is not None
            else None
        )
        drafts = await self.stores.drafts.list_drafts_for_job(job.id, job.organization_id)
        draft = drafts[0] if drafts else None
        audit = await self._audit(job, fed, case, reached_drafting)

        timings = stage_timings(events)
        retrieved = _retrieved(fed, context or {})
        record = self._record(draft, audit, reached_drafting, timings)
        record["final_body"] = draft.body if draft is not None else ""
        record["final_action"] = draft.action if draft is not None else "none"
        record["retrieved"] = retrieved
        record["host"] = {
            "classification_category": classification.category if classification else None,
            "retrieval_query": None,  # the ai-worker does not persist the query it built
            "kb_docs_ingested": len(fed.docs),
            "poison_ingested": any(doc.poisoned for doc in fed.docs),
            "poison_retrieved": any(entry["poisoned"] for entry in retrieved),
            "retrieved": retrieved,
            "context_ms": timings["context"] or 0,
        }
        ctx = context or {}
        record["pipeline"] = {
            "transport": TRANSPORT,
            "job_state": job.state,
            "triage": _triage_block(classification, gate_outcome(events)),
            "reached_drafting": reached_drafting,
            "summary_triggered": ctx.get("summary_triggered"),
            "rerank_applied": ctx.get("rerank_applied"),
            "retrieval_degraded": ctx.get("retrieval_degraded"),
            "retrieval_underfilled": ctx.get("retrieval_underfilled"),
            "timings_ms": timings,
        }
        return record

    async def _audit(
        self, job: Job, fed: FedCase, case: EvalCase, reached_drafting: bool
    ) -> dict[str, Any] | None:
        """The guard-worker's audit result of a drafted, guarded job; None when none is due."""
        if self.config == NATIVE_CONFIG or not reached_drafting or self.audit_path is None:
            return None
        message_id = job.message_id if job.message_id is not None else fed.provider_message_id
        row = await wait_for_audit(
            self.audit_path,
            organization_id=fed.organization_id,
            message_id=message_id,
            config=self.config,
            grace_s=self.audit_grace_s,
            sleep=self.sleep,
            poll_interval_s=self.poll_interval_s,
            monotonic=self.monotonic,
        )
        if row is None:
            raise AuditMissingError(
                f"case {case.case_id}: job {job.id} was drafted but {self.audit_path.name} has no "
                f"line for message {message_id}; is the {self.config} guard-worker the drafting "
                "consumer, and not the ai-worker container?"
            )
        return _audit_result(row)

    def _record(
        self,
        draft: GeneratedDraft | None,
        audit: Mapping[str, Any] | None,
        reached_drafting: bool,
        timings: Mapping[str, int | None],
    ) -> dict[str, Any]:
        """The v1 blocks of the row: the audit line's for a guarded case, else the draft's."""
        generation_ms = timings["generation"] or 0
        instructions = DefaultInstructionProvider.DEFAULT_AGENT_INSTRUCTIONS
        record = _unguarded_record(str(instructions))
        if audit is None:
            called = draft is not None and reached_drafting
            block, final_draft = _draft_blocks(draft)
            record.update(
                {
                    "prompt_mode": NATIVE_PROMPT_MODE if called else None,
                    "generation": _generation_block(draft, called=called, latency_ms=generation_ms),
                    "draft": block,
                    "final_draft": final_draft,
                    "timings_ms": {
                        "guarded_total": generation_ms,
                        "generation": generation_ms,
                        "guard": 0,
                    },
                }
            )
            return record
        record.update(audit)
        generation = audit.get("generation")
        if not isinstance(generation, Mapping):
            generation = _generation_block(
                draft, called=draft is not None and reached_drafting, latency_ms=generation_ms
            )
            record["generation"] = generation
        reply_v1 = generation.get("reply_v1")
        original = reply_v1.get("draft") if isinstance(reply_v1, Mapping) else None
        block, final_draft = _draft_blocks(
            draft, original=str(original) if original is not None else None
        )
        # The persisted draft is what a reviewer would see; the audit line's own draft blocks,
        # when it has them, describe the same draft in more detail (pre-L4 body).
        if not isinstance(audit.get("draft"), Mapping):
            record["draft"] = block
        if "final_draft" not in audit:
            record["final_draft"] = final_draft
        record["blocked_inbound"] = bool(audit.get("blocked_inbound"))
        record["blocked_outbound"] = bool(audit.get("blocked_outbound"))
        record["blocked"] = bool(
            audit.get("blocked", record["blocked_inbound"] or record["blocked_outbound"])
        )
        if record["blocked"]:
            record["final_draft"] = None  # a blocked case has no final draft, as in v1
        record["guard_errors"] = [str(error) for error in audit.get("guard_errors") or []]
        record.setdefault(
            "timings_ms", {"guarded_total": generation_ms, "generation": generation_ms, "guard": 0}
        )
        return record
