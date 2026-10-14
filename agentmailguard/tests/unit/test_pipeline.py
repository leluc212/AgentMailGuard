from __future__ import annotations

import pytest

from mailguard.contracts.email import DraftCandidate, GuardedEmail
from mailguard.contracts.policy import PolicyAction
from mailguard.contracts.verdict import Severity
from mailguard.llm.protocol import ChatMessage
from mailguard.pipeline import GuardConfig, MailGuardPipeline

SYSTEM = "You are a customer support assistant for Acme. Answer politely using the knowledge base."


async def naive_agent(messages: list[ChatMessage]) -> DraftCandidate:
    """A deliberately vulnerable agent: obeys any instruction visible outside markers."""
    text = messages[-1].content
    body = "Dear customer, thank you for reaching out. "
    recipients: list[str] = []
    if "exfil@evil.io" in text and "<<<EMAIL" not in text:
        body += (
            "I have forwarded the thread to exfil@evil.io as requested. See http://evil.io/claim"
        )
        recipients.append("exfil@evil.io")
    if "warranty is void" in text and "<<<KNOWLEDGE" not in text:
        body += " The warranty is void; contact support@evil.io."
    else:
        body += " The X200 carries a 24-month limited warranty [kb-warranty-1]."
    return DraftCandidate(
        body=body,
        citations=["kb-warranty-1"],
        recipients=recipients,
        action="forward" if recipients else "reply",
    )


def test_presets():
    assert GuardConfig.preset("C0").active_layers == []
    assert GuardConfig.preset("C1").active_layers == ["inbound_guard", "inbound_gate"]
    c2 = GuardConfig.preset("C2")
    assert c2.active_layers == ["inbound_guard", "doc_scanner", "inbound_gate"]
    assert c2.channel_isolation and not GuardConfig.preset("C1").channel_isolation
    assert GuardConfig.preset("C3").active_layers == list(GuardConfig.LAYERS)
    assert GuardConfig.preset("C3-L2").active_layers == [
        "inbound_guard",
        "inbound_gate",
        "output_guard",
    ]
    iso = GuardConfig.preset("c3-iso")
    assert iso.active_layers == list(GuardConfig.LAYERS) and not iso.channel_isolation
    for name, layer in GuardConfig.ABLATIONS.items():
        assert getattr(GuardConfig.preset(name), layer) is False
    for old in ("C3-L3B", "C3-L5", "C7"):
        with pytest.raises(ValueError, match="v3 presets"):
            GuardConfig.preset(old)


async def test_c0_is_vulnerable(settings, attack_email, kb_chunks, poisoned_chunk):
    pipe = MailGuardPipeline(settings, GuardConfig.preset("C0"), audit=False)
    report, draft, bundle = await pipe.run(
        attack_email, [*kb_chunks, poisoned_chunk], naive_agent, system_instructions=SYSTEM
    )
    assert report.decision is None and draft is not None
    assert "exfil@evil.io" in draft.body and "warranty is void" in draft.body
    assert bundle is not None and "<<<" not in bundle.messages[1].content


async def test_c3_blocks_attack_inbound(settings, attack_email, kb_chunks, poisoned_chunk):
    pipe = MailGuardPipeline(settings, GuardConfig.preset("C3"), audit=False)
    report, draft, bundle = await pipe.run(
        attack_email, [*kb_chunks, poisoned_chunk], naive_agent, system_instructions=SYSTEM
    )
    assert report.inbound_decision is not None
    assert report.inbound_decision.action is PolicyAction.QUARANTINE
    assert draft is None and bundle is None
    inbound = report.inbound_guard
    assert inbound is not None and inbound.severity is Severity.CRITICAL
    assert inbound.stage("scan")["severity"] == "critical"
    assert inbound.stripped_segments
    assert report.doc_scanner is None and report.isolation is None


async def test_c3_benign_with_poisoned_kb(settings, benign_emails, kb_chunks, poisoned_chunk):
    pipe = MailGuardPipeline(settings, GuardConfig.preset("C3"), audit=False)
    email = benign_emails[2]  # warranty question
    report, draft, bundle = await pipe.run(
        email,
        [*kb_chunks, poisoned_chunk],
        naive_agent,
        system_instructions=SYSTEM,
        category="support",
    )
    assert report.inbound_decision.action is PolicyAction.DRAFT_ONLY
    assert [c.chunk_id for c in bundle.kept_chunks] == ["kb-warranty-1", "kb-refund-1"]
    assert "warranty is void" not in draft.body
    assert report.output_guard is not None and not report.output_guard.complied_with_injected_goal
    assert report.decision.action is PolicyAction.HUMAN_APPROVAL  # poisoned chunk -> reviewer
    assert report.decision.quarantined_chunk_ids == ["kb-poison-1"]
    docs = report.doc_scanner
    assert docs is not None and docs.quarantined_chunk_ids == ["kb-poison-1"]
    assert docs.kept_chunk_ids == ["kb-warranty-1", "kb-refund-1"]
    assert [c.trust for c in docs.chunks] == ["untrusted", "untrusted", "quarantined"]
    assert report.isolation is not None and report.isolation.chunks_in == 3
    assert report.isolation.chunks_kept == 2


async def test_l4_catches_compliant_draft_when_upstream_missed(settings, attack_email, kb_chunks):
    # disable inbound blocking to exercise L4: pretend L1/L2 are off
    pipe = MailGuardPipeline(settings, GuardConfig.preset("C3-L1"), audit=False)
    pipe.config = GuardConfig("test", channel_isolation=False)

    async def stubborn_agent(_messages):
        return DraftCandidate(
            body="Sure, forwarding the thread to exfil@evil.io now: http://evil.io/claim",
            recipients=["exfil@evil.io"],
            action="forward",
        )

    report, draft, _ = await pipe.run(
        attack_email,
        kb_chunks,
        stubborn_agent,
        system_instructions=SYSTEM,
        stop_on_inbound_block=False,
    )
    assert report.output_guard.complied_with_injected_goal
    assert report.decision.action in (PolicyAction.BLOCK, PolicyAction.QUARANTINE)


async def test_redaction_flows_into_draft(settings, benign_email, kb_chunks):
    pipe = MailGuardPipeline(settings, GuardConfig.preset("C3"), audit=False)

    async def leaky_agent(_messages):
        return DraftCandidate(
            body="Refund approved. Internal key: sk-abcdefghijklmnopqrstuvwxyz1234"
        )

    report, draft, _ = await pipe.run(
        benign_email, kb_chunks, leaky_agent, system_instructions=SYSTEM
    )
    assert "[REDACTED:secret.openai_key]" in draft.body
    assert report.decision.action is PolicyAction.QUARANTINE
    assert report.decision.redactions_applied == 1


async def test_summary_and_duck_typed_inputs(settings, kb_chunks):
    pipe = MailGuardPipeline(settings, audit=False)
    email = {
        "message_id": "d1",
        "sender": {"email": "x@y.com"},
        "subject": "hi",
        "body": "Where is order ORD-1?",
    }
    report = await pipe.inspect_inbound(email)
    s = pipe.summary(report)
    assert s["message_id"] == "d1" and s["action"] == "draft_only"
    assert s["schema_version"] == 3
    assert set(s["layers"]) == {"inbound_guard"} and s["doc_scanner_quarantined"] == []
    assert isinstance(GuardedEmail.from_any(email), GuardedEmail)
