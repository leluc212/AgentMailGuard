"""L3 Inbound Gate (ADR-0015 Task 7): two passes, one audit line, no model call when it stops."""

from __future__ import annotations

import json
from pathlib import Path

from mailguard.contracts.policy import PolicyAction
from mailguard.llm.fake import FakeLLMProvider
from mailguard.pipeline import ESCALATED_BODY, GuardConfig, MailGuardPipeline

SYSTEM = "You are the Acme support assistant."


def _audit_lines(pipe: MailGuardPipeline) -> list[dict]:
    path = Path(pipe.policy.audit_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_an_email_l1_blocks_never_reaches_the_doc_judge(settings, attack_email, kb_chunks):
    settings.doc_scanner.llm_enabled = True
    judge = FakeLLMProvider(default_response={"is_poisoned": False, "confidence": 0.9})
    pipe = MailGuardPipeline(settings, GuardConfig.preset("C3"), doc_judge=judge)
    report = await pipe.inspect_inbound(attack_email, kb_chunks)
    assert pipe.blocked(report.inbound_decision)
    assert report.inbound_decision.metadata["gate_passes"] == 1
    assert report.doc_scanner is None and judge.calls == []
    assert len(_audit_lines(pipe)) == 1


async def test_pass_2_sees_the_documents_and_is_the_one_audited(
    settings, benign_emails, kb_chunks, poisoned_chunk
):
    pipe = MailGuardPipeline(settings, GuardConfig.preset("C3"))
    report = await pipe.inspect_inbound(benign_emails[2], [*kb_chunks, poisoned_chunk])
    decision = report.inbound_decision
    assert decision.metadata["gate_passes"] == 2
    assert report.doc_scanner is not None
    assert report.doc_scanner.quarantined_chunk_ids == ["kb-poison-1"]
    assert decision.action is PolicyAction.HUMAN_APPROVAL  # decided before generation
    lines = _audit_lines(pipe)
    assert len(lines) == 1 and lines[0]["audit_id"] == decision.audit_id


async def test_the_audit_id_is_stable_across_identical_runs(settings, benign_emails, kb_chunks):
    first = MailGuardPipeline(settings, GuardConfig.preset("C3"), audit=False)
    second = MailGuardPipeline(settings, GuardConfig.preset("C3"), audit=False)
    a = await first.inspect_inbound(benign_emails[0], kb_chunks)
    b = await second.inspect_inbound(benign_emails[0], kb_chunks)
    assert a.inbound_decision.audit_id == b.inbound_decision.audit_id
    without_docs = await first.inspect_inbound(benign_emails[0])
    assert without_docs.inbound_decision.metadata["gate_passes"] == 1
    assert without_docs.inbound_decision.audit_id != a.inbound_decision.audit_id


async def test_no_model_call_when_the_gate_stops_the_job(settings, attack_email, kb_chunks):
    calls: list[object] = []

    async def generate(messages):
        calls.append(messages)
        return "draft"

    pipe = MailGuardPipeline(settings, GuardConfig.preset("C3"), audit=False)
    report, draft, bundle = await pipe.run(
        attack_email, kb_chunks, generate, system_instructions=SYSTEM
    )
    assert (draft, bundle) == (None, None) and calls == []
    escalated = pipe.build_escalated_draft(report)
    assert escalated.action == "escalate" and escalated.body == ESCALATED_BODY
    assert report.inbound_decision.matched_rule_id in escalated.subject
    assert "IGNORE" not in escalated.body


async def test_kept_chunks_are_rendered_with_their_trust(
    settings, benign_emails, kb_chunks, poisoned_chunk
):
    pipe = MailGuardPipeline(settings, GuardConfig.preset("C3"), audit=False)
    chunks = [*kb_chunks, poisoned_chunk]
    report = await pipe.inspect_inbound(benign_emails[2], chunks)
    bundle = await pipe.build_prompt(report, benign_emails[2], chunks, system_instructions=SYSTEM)
    user = bundle.messages[1].content
    assert "kb-poison-1" not in user
    assert "[chunk^id=kb-warranty-1^trust=" in user  # datamarked
    assert [c.chunk_id for c in bundle.kept_chunks] == ["kb-warranty-1", "kb-refund-1"]
