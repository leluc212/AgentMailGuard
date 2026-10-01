from __future__ import annotations

from mailguard.contracts.email import DraftCandidate, GuardedEmail, RetrievedChunk
from mailguard.contracts.policy import PolicyAction, RiskTier
from mailguard.contracts.verdict import Severity


def test_severity_ladder_and_from_score():
    assert Severity.from_score(0.0) is Severity.NONE
    assert Severity.from_score(0.25) is Severity.LOW
    assert Severity.from_score(0.6) is Severity.MEDIUM
    assert Severity.from_score(0.9) is Severity.HIGH
    assert Severity.from_score(0.99) is Severity.CRITICAL
    assert Severity.max(Severity.LOW, Severity.HIGH, Severity.NONE) is Severity.HIGH
    assert Severity.from_score(0.55, flag=0.6, block=0.9) is Severity.LOW


def test_policy_action_ordering():
    assert PolicyAction.strictest(PolicyAction.DRAFT_ONLY, PolicyAction.BLOCK) is PolicyAction.BLOCK
    assert PolicyAction.strictest() is PolicyAction.DRAFT_ONLY
    assert RiskTier.from_severity(Severity.HIGH) is RiskTier.T3_HIGH
    assert RiskTier.T4_CRITICAL.rank > RiskTier.T0_CLEAN.rank


def test_guarded_email_from_dict_and_object():
    e = GuardedEmail.from_any(
        {
            "message_id": "x",
            "sender": {"email": "A@B.com", "name": "A"},
            "subject": "s",
            "body": "hello",
            "recipients": [{"email": "Support@Acme.com"}],
            "headers": {"X-Test": "1"},
        }
    )
    assert e.sender_email == "a@b.com"
    assert e.sender_name == "A"
    assert e.recipients == ["support@acme.com"]
    assert e.body_text == "hello"
    assert e.headers == {"x-test": "1"}

    class Obj:
        message_id = "y"
        sender_email = "z@z.com"
        subject = "obj"
        body_text = "raw"
        body_text_clean = "clean"

    o = GuardedEmail.from_any(Obj())
    assert o.text == "clean"
    assert "raw" in o.full_text


def test_chunk_and_draft_duck_typing():
    c = RetrievedChunk.from_any({"id": "c1", "text": "content", "external_id": "DOC-1"})
    assert c.chunk_id == "c1" and c.citation_id == "DOC-1"
    d = DraftCandidate.from_any({"draft": "body", "knowledge_chunks": [{"chunk_id": "c1"}, "c2"]})
    assert d.body == "body"
    assert d.citations == ["c1", "c2"]
