"""DispatchService: design §5.8's five steps on the fake adapter (tasks 6.5, 6.6, 6.7).

Requirements: R17.1 (modes), R17.3 (idempotency key), R17.4 (provider ref, COMPLETED),
R17.5 (transient vs permanent), R17.6 (approval before send), R17.7 (outbound write-back),
R19.2 (key derivation), R19.3 (exactly one provider send under redelivery).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from uuid import uuid4

import pytest

from packages.adapters.exceptions import AuthExpired, RateLimited, Transient
from packages.core.idempotency import derive_idempotency_key
from packages.db.dispatch import DispatchKeyConflictError
from packages.dispatch.reply import MissingProviderThreadError
from packages.dispatch.service import (
    DispatchJobBusyError,
    DispatchOutcome,
    DispatchPermanentError,
    bare_message_id,
    message_id_domain,
)
from packages.domain import DispatchMode, ProviderDraftStatus
from packages.domain.entities import GeneratedDraft
from packages.domain.state_machine import JobState
from tests.stubs.dispatch_fakes import DispatchWorld, build_dispatch_world

SEND = DispatchMode.SEND_REPLY


async def _dispatch(w: DispatchWorld) -> DispatchOutcome:
    return await w.service.dispatch(organization_id=w.org_id, job_id=w.job_id)


async def _state(w: DispatchWorld) -> str:
    job = await w.store.jobs.get_job(w.org_id, w.job_id)
    assert job is not None
    return job.state


async def _states(w: DispatchWorld) -> list[str]:
    return [e.state_to for e in await w.store.jobs.list_events_for_job(w.org_id, w.job_id)]


def _key(w: DispatchWorld) -> str:
    return derive_idempotency_key(
        organization_id=w.org_id,
        mailbox_id=w.mailbox.id,
        provider_message_id=w.original.provider_message_id,
        operation_type="dispatch",
    )


async def test_create_draft_mode_stops_after_the_provider_draft() -> None:
    """R17.1 / 6.5: default mode creates one provider draft and records no outbound message."""
    w = await build_dispatch_world()
    assert await _dispatch(w) is DispatchOutcome.COMPLETED_DRAFT

    assert w.fake.calls["create_draft"] == 1
    assert w.fake.calls["send_draft"] == 0
    assert w.fake.calls["find_draft"] == 0  # a fresh DRAFTED claim has no orphan to look for
    assert await _state(w) == JobState.COMPLETED.value
    assert (await _states(w))[-2:] == [JobState.DISPATCHED.value, JobState.COMPLETED.value]
    job = await w.store.jobs.get_job(w.org_id, w.job_id)
    assert job is not None and job.queue_name == "email.dispatch"
    draft = w.store.drafts[w.draft_id]
    assert draft.status == "dispatched"
    assert draft.provider_draft_id
    assert draft.provider_ref == draft.provider_draft_id
    assert draft.dispatch_idempotency_key == _key(w)
    assert w.store.outbound == []


async def test_send_reply_mode_sends_once_and_writes_back_the_outbound_message() -> None:
    """R17.4 / R17.7: one send, provider id persisted, outbound row and thread updated."""
    w = await build_dispatch_world(mode=SEND)
    assert await _dispatch(w) is DispatchOutcome.COMPLETED_SENT

    assert w.fake.calls["send_draft"] == 1
    [sent] = list(w.fake.sent.values())
    [reply] = list(w.fake.replies.values())
    [outbound] = w.store.outbound
    assert outbound.direction == "outbound"
    assert outbound.provider_message_id == sent.provider_message_id
    assert outbound.rfc822_message_id == bare_message_id(reply.message_id)
    assert outbound.rfc822_message_id and "<" not in outbound.rfc822_message_id
    assert outbound.in_reply_to == "orig-1@customer.example"
    assert outbound.thread_id == w.thread.id
    assert outbound.sender.email == "support@acme.example"
    assert w.store.threads[w.thread.id].message_count == w.thread.message_count + 1
    draft = w.store.drafts[w.draft_id]
    assert draft.status == "dispatched"
    assert draft.provider_ref == sent.provider_message_id
    assert await _state(w) == JobState.COMPLETED.value


async def test_redelivery_after_completion_sends_nothing() -> None:
    """R19.3: a replayed dispatch job after COMPLETED is acknowledged with no provider call."""
    w = await build_dispatch_world(mode=SEND)
    await _dispatch(w)
    calls_before = dict(w.fake.calls)
    assert await _dispatch(w) is DispatchOutcome.ALREADY_DONE
    assert dict(w.fake.calls) == calls_before
    assert len(w.store.outbound) == 1


async def test_unapproved_draft_is_never_dispatched() -> None:
    """R16.8 / R17.6: without approval (and no auto-send) nothing is claimed or sent."""
    w = await build_dispatch_world(mode=SEND, draft_status="draft")
    assert await _dispatch(w) is DispatchOutcome.NOT_DISPATCHABLE
    assert await _state(w) == JobState.DRAFTED.value
    assert sum(w.fake.calls.values()) == 0


async def test_auto_send_category_dispatches_without_approval() -> None:
    """R17.6: auto_send_eligible opts a category out of the approval requirement."""
    w = await build_dispatch_world(mode=SEND, draft_status="draft", auto_send=True)
    assert await _dispatch(w) is DispatchOutcome.COMPLETED_SENT
    assert w.fake.calls["send_draft"] == 1


async def test_rate_limit_before_the_draft_leaves_the_job_dispatched() -> None:
    """R17.5: a 429 propagates with its Retry-After; the retry then completes."""
    w = await build_dispatch_world()
    w.fake.inject_rate_limit(retry_after=120.0)
    with pytest.raises(RateLimited) as exc_info:
        await _dispatch(w)
    assert exc_info.value.retry_after == 120.0
    assert await _state(w) == JobState.DISPATCHED.value
    assert w.store.drafts[w.draft_id].provider_draft_id is None

    assert await _dispatch(w) is DispatchOutcome.COMPLETED_DRAFT
    assert w.fake.calls["create_draft"] == 1


async def test_ambiguous_send_failure_is_confirmed_not_resent() -> None:
    """Step 4: the provider accepted the send, the call failed; the retry confirms instead."""
    w = await build_dispatch_world(mode=SEND)
    w.fake.fail_send = "after"
    with pytest.raises(Transient):
        await _dispatch(w)
    assert w.fake.calls["send_draft"] == 1
    assert await _state(w) == JobState.DISPATCHED.value

    assert await _dispatch(w) is DispatchOutcome.COMPLETED_SENT
    assert w.fake.calls["send_draft"] == 1
    assert w.fake.calls["get_draft_status"] == 1
    [reply] = list(w.fake.replies.values())
    draft = w.store.drafts[w.draft_id]
    # Draft message id first (Graph keeps it), then our Message-ID (Gmail's new id).
    assert w.fake.find_queries == [draft.provider_draft_message_id, reply.message_id]
    [sent] = list(w.fake.sent.values())
    assert [m.provider_message_id for m in w.store.outbound] == [sent.provider_message_id]


async def test_send_that_never_happened_is_sent_on_retry_after_one_recheck() -> None:
    """Step 4: DRAFT on resume is re-checked once after the delay, then sent exactly once."""
    w = await build_dispatch_world(mode=SEND, recheck_delay_s=0.01)
    w.fake.fail_send = "before"
    with pytest.raises(Transient):
        await _dispatch(w)
    assert w.fake.calls["send_draft"] == 0

    assert await _dispatch(w) is DispatchOutcome.COMPLETED_SENT
    assert w.fake.calls["get_draft_status"] == 2
    assert w.fake.calls["send_draft"] == 1
    assert w.fake.calls["create_draft"] == 1


async def test_missing_draft_without_a_sent_copy_is_permanent() -> None:
    """Step 4: MISSING and no sent message in the thread means a person deleted the draft."""
    w = await build_dispatch_world(mode=SEND)
    w.fake.fail_send = "after"
    with pytest.raises(Transient):
        await _dispatch(w)
    w.fake.hide_sent = True
    with pytest.raises(DispatchPermanentError):
        await _dispatch(w)
    assert w.fake.calls["send_draft"] == 1
    assert await _state(w) == JobState.DISPATCHED.value  # the consumer dead-letters it
    assert w.store.outbound == []


async def test_provider_reported_sent_finishes_without_sending() -> None:
    """Step 4 (Graph semantics): SENT goes straight to finish with the draft's message id."""
    w = await build_dispatch_world(
        mode=SEND, job_state=JobState.DISPATCHED, provider_draft_id="d-1"
    )
    w.fake.status_override = ProviderDraftStatus.SENT
    assert await _dispatch(w) is DispatchOutcome.COMPLETED_SENT
    assert w.fake.calls["send_draft"] == 0
    assert w.fake.calls["find_sent_message"] == 0
    assert [m.provider_message_id for m in w.store.outbound] == ["d-1-msg"]


async def test_existing_provider_draft_is_reused_not_recreated() -> None:
    """Step 2: a crash after the draft was recorded resumes without a second provider draft."""
    w = await build_dispatch_world(job_state=JobState.DISPATCHED, provider_draft_id="d-1")
    assert await _dispatch(w) is DispatchOutcome.COMPLETED_DRAFT
    assert w.fake.calls["create_draft"] == 0
    assert w.store.drafts[w.draft_id].provider_ref == "d-1"


async def test_null_provider_thread_id_fails_after_the_claim() -> None:
    """6.3 / 6.6: an IMAP-style thread without a provider id is permanent; nothing is sent."""
    w = await build_dispatch_world(mode=SEND, provider_thread_id=None)
    with pytest.raises(MissingProviderThreadError):
        await _dispatch(w)
    assert await _state(w) == JobState.DISPATCHED.value
    assert sum(w.fake.calls.values()) == 0


async def test_operator_replay_resumes_from_retry_pending() -> None:
    """6.5: RETRY_PENDING -> DISPATCHED (operator replay) without regenerating."""
    w = await build_dispatch_world(job_state=JobState.RETRY_PENDING)
    assert await _dispatch(w) is DispatchOutcome.COMPLETED_DRAFT
    states = await _states(w)
    assert states[-2:] == [JobState.DISPATCHED.value, JobState.COMPLETED.value]
    assert JobState.GENERATING.value not in states


async def test_key_held_by_another_draft_is_a_conflict() -> None:
    """R19.2: the UNIQUE dispatch key cannot be claimed twice; the job is not moved."""
    w = await build_dispatch_world()
    w.store.add_draft(
        GeneratedDraft(
            organization_id=w.org_id,
            message_id=w.original.message_id,
            thread_id=w.thread.id,
            job_id=uuid4(),
            body="another draft for the same email",
            status="approved",
            dispatch_idempotency_key=_key(w),
        )
    )
    with pytest.raises(DispatchKeyConflictError):
        await _dispatch(w)
    assert await _state(w) == JobState.DRAFTED.value


async def test_token_expiry_mid_dispatch_is_permanent_and_replay_reuses_the_draft() -> None:
    """Review focus 3: a 401 between the provider draft and the send dead-letters; after the
    owner refreshes the token, the operator replay sends once with the recorded draft."""
    w = await build_dispatch_world(mode=SEND)
    w.fake.inject_auth_expired(method="send_draft")
    with pytest.raises(AuthExpired):
        await _dispatch(w)
    assert w.fake.calls["create_draft"] == 1
    assert w.fake.calls["send_draft"] == 0
    assert await _state(w) == JobState.DISPATCHED.value  # the consumer dead-letters it
    assert w.store.drafts[w.draft_id].provider_draft_id is not None
    assert w.store.outbound == []

    # What the consumer and the operator do: DISPATCHED -> FAILED -> DEAD_LETTER -> RETRY_PENDING.
    for target in (JobState.FAILED, JobState.DEAD_LETTER, JobState.RETRY_PENDING):
        await w.store.jobs.transition_job_state(w.org_id, w.job_id, target)
    assert await _dispatch(w) is DispatchOutcome.COMPLETED_SENT
    assert w.fake.calls["create_draft"] == 1
    assert w.fake.calls["send_draft"] == 1
    assert len(w.store.outbound) == 1


async def test_provider_draft_deleted_before_the_send_is_not_recreated() -> None:
    """Review focus 4: a person deletes the provider draft after it was recorded and before
    the send; the resumed dispatch finds neither the draft nor a sent copy and dead-letters."""
    w = await build_dispatch_world(mode=SEND)
    w.fake.inject_transient_failure(method="send_draft")
    with pytest.raises(Transient):
        await _dispatch(w)
    [provider_draft_id] = list(w.fake.replies)
    w.fake.delete_draft(provider_draft_id)

    with pytest.raises(DispatchPermanentError):
        await _dispatch(w)
    assert w.fake.calls["create_draft"] == 1
    assert w.fake.calls["send_draft"] == 0
    assert await _state(w) == JobState.DISPATCHED.value  # the consumer dead-letters it
    assert w.store.outbound == []


async def test_dispatch_mode_is_read_when_the_dispatch_runs() -> None:
    """Review focus 5: the category switched to send_reply after approve; the dispatch uses
    the mode in force when it runs and sends exactly once."""
    w = await build_dispatch_world(mode=DispatchMode.CREATE_DRAFT)
    definition = w.registry.get("billing")
    assert definition is not None
    w.registry.register_category(replace(definition, dispatch_mode=SEND))

    assert await _dispatch(w) is DispatchOutcome.COMPLETED_SENT
    assert w.fake.calls["create_draft"] == 1
    assert w.fake.calls["send_draft"] == 1


async def test_mode_switch_after_a_completed_draft_does_not_send() -> None:
    """Review focus 5: a job completed in create_draft mode is not sent when the category
    later switches to send_reply and the dispatch job is redelivered."""
    w = await build_dispatch_world()
    assert await _dispatch(w) is DispatchOutcome.COMPLETED_DRAFT
    definition = w.registry.get("billing")
    assert definition is not None
    w.registry.register_category(replace(definition, dispatch_mode=SEND))

    assert await _dispatch(w) is DispatchOutcome.ALREADY_DONE
    assert w.fake.calls["send_draft"] == 0
    assert w.store.outbound == []


async def test_second_delivery_of_the_same_job_is_refused_while_the_first_runs() -> None:
    """R19.3: two deliveries of one job never run the steps together (a repeated approve
    re-publishes while DISPATCHED; the consumer runs several deliveries at once)."""
    w = await build_dispatch_world(mode=SEND)
    w.fake.pause_at = "before_send_draft"
    first = asyncio.create_task(_dispatch(w))
    await asyncio.wait_for(w.fake.paused.wait(), timeout=5)

    with pytest.raises(DispatchJobBusyError):
        await _dispatch(w)
    assert w.fake.calls["create_draft"] == 1

    w.fake.resume.set()
    assert await first is DispatchOutcome.COMPLETED_SENT
    assert await _dispatch(w) is DispatchOutcome.ALREADY_DONE
    assert w.fake.calls["create_draft"] == 1
    assert w.fake.calls["send_draft"] == 1
    assert len(w.store.outbound) == 1


@pytest.mark.parametrize("mode", [DispatchMode.CREATE_DRAFT, SEND])
async def test_crash_after_create_draft_adopts_the_unrecorded_provider_draft(
    mode: DispatchMode,
) -> None:
    """tasks.md 6.5: the provider accepted create_draft, the worker died before recording the
    id; the redelivery finds that draft by our Message-ID and never creates a second one."""
    w = await build_dispatch_world(mode=mode)
    w.fake.crash_at = "after_create_draft"
    crashed = asyncio.create_task(_dispatch(w))
    await asyncio.wait_for(w.fake.crash_reached.wait(), timeout=5)
    crashed.cancel()  # the worker dies; its per-job lock goes with it
    with contextlib.suppress(asyncio.CancelledError):
        await crashed
    assert w.store.drafts[w.draft_id].provider_draft_id is None
    assert await _state(w) == JobState.DISPATCHED.value

    expected = DispatchOutcome.COMPLETED_SENT if mode is SEND else DispatchOutcome.COMPLETED_DRAFT
    assert await _dispatch(w) is expected
    [orphan] = list(w.fake.refs)
    assert w.fake.calls["create_draft"] == 1
    assert w.fake.calls["find_draft"] == 1
    assert w.store.drafts[w.draft_id].provider_draft_id == orphan
    if mode is SEND:
        # Not created by this delivery, so step 4 confirms before the one send.
        assert w.fake.calls["get_draft_status"] == 1
        assert w.fake.calls["send_draft"] == 1
        assert len(w.store.outbound) == 1


def test_message_id_helpers() -> None:
    assert message_id_domain("support@Acme.Example") == "acme.example"
    assert bare_message_id("<abc@acme.example>") == "abc@acme.example"
    assert bare_message_id("  ") is None
    assert bare_message_id(None) is None
