"""L1 Inbound Guard facade (ADR-0015 Task 5): merge rules, R-i, F4, question sentences."""

from __future__ import annotations

import pytest

from mailguard.contracts.email import GuardedEmail
from mailguard.contracts.verdict import Severity
from mailguard.layers.inbound_guard import InboundGuard
from mailguard.llm.fake import FakeLLMProvider
from mailguard.llm.protocol import LLMResponseError
from mailguard.pipeline import GuardConfig, MailGuardPipeline
from mailguard.prompting.questions import MAX_SENTENCES, question_sentences

FLAGGING_EXTRACTOR = {
    "user_intent": "The customer says the earlier cancellation email can be ignored.",
    "requested_actions": [],
    "entities": {},
    "contains_assistant_instructions": True,
    "instructions_to_assistant": ["please ignore my previous email"],
    "confidence": 0.9,
}
JUDGE_CLEAN = {
    "is_injection": False,
    "confidence": 0.9,
    "techniques": [],
    "injected_instructions": [],
    "rationale": "ordinary",
}


def _prompt_names(llm: FakeLLMProvider) -> list[str]:
    from mailguard.prompts import load_prompt

    names = {load_prompt(n): n for n in ("inbound_scan_judge.v1", "inbound_extractor.v1")}
    return [names.get(c["messages"][0].content, "?") for c in llm.calls]


def test_facade_shares_rules_and_classifier(settings):
    guard = InboundGuard(settings)
    assert guard.extract.rules is guard.scan.rules is guard.rules
    assert guard.extract.classifier is guard.scan.classifier is guard.classifier
    assert guard.judge is None and guard.extractor_llm is None
    judge = FakeLLMProvider(default_response=JUDGE_CLEAN)
    guard.judge = judge
    assert guard.scan.judge is judge


async def test_clean_scan_and_flagging_extractor_keep_severity_and_add_a_finding(
    settings, benign_emails
):
    """R-i / ADR-0015 bullet 4: the extractor's flag is a finding, never a severity."""
    email = benign_emails[3]  # b-004: "please ignore my previous email ..." (harmless)
    llm = FakeLLMProvider(default_response=FLAGGING_EXTRACTOR)
    verdict = await InboundGuard(settings, extractor_llm=llm).inspect(email)
    assert verdict is not None
    assert verdict.severity is Severity.NONE and verdict.score == 0.0
    assert verdict.stage("scan")["severity"] == "none"
    flagged = [f for f in verdict.findings if f.rule_id == "assistant_instructions"]
    assert len(flagged) == 1
    assert flagged[0].detector == "extract" and flagged[0].metadata["stage"] == "extract"


async def test_severity_comes_from_the_scan_only(settings, attack_email):
    verdict = await InboundGuard(settings).inspect(attack_email)
    assert verdict is not None
    assert verdict.severity is Severity.CRITICAL
    assert str(verdict.severity) == verdict.stage("scan")["severity"]
    assert verdict.score == verdict.stage("scan")["score"]
    assert verdict.stripped_segments  # extract still strips the injected paragraph


async def test_without_the_scan_the_extract_stage_grades_until_task_6(settings, attack_email):
    """F11 transitional rule: v2 preset C3-L1 (scan off, extract on) keeps v2 grading."""
    verdict = await InboundGuard(settings).inspect(attack_email, run_scan=False)
    assert verdict is not None and verdict.stage("scan") is None
    assert str(verdict.severity) == verdict.stage("extract")["severity"]
    assert verdict.severity.rank >= Severity.HIGH.rank
    assert verdict.stripped_segments and verdict.removed_ratio > 0.3


async def test_extract_runs_deterministically_without_its_llm(settings, attack_email):
    """F4: with the extract LLM off, the verdict still has a sanitized body and removed_ratio."""
    settings.inbound_guard.extract_llm_enabled = False
    llm = FakeLLMProvider(default_response=FLAGGING_EXTRACTOR)
    guard = InboundGuard(settings, extractor_llm=llm)
    verdict = await guard.inspect(attack_email)
    assert verdict is not None
    assert verdict.sanitized_body and "IGNORE ALL PREVIOUS" not in verdict.sanitized_body
    assert verdict.removed_ratio > 0.3
    assert llm.calls == []
    assert verdict.stage("extract")["metadata"]["llm_used"] is False


async def test_at_most_two_model_calls_per_email(settings):
    """Q5=a: one scan-judge call (uncertain band only) and one extractor call."""
    borderline = GuardedEmail(
        subject="Renewal letter",
        body_text="My renewal letter says failure to comply with the new terms ends my plan. "
        "Could you explain what I need to do for order #55120?",
    )
    from mailguard.prompts import load_prompt

    judge_prompt = load_prompt("inbound_scan_judge.v1")
    llm = FakeLLMProvider(
        responder=lambda messages, _schema: (
            JUDGE_CLEAN if messages[0].content == judge_prompt else FLAGGING_EXTRACTOR
        )
    )
    verdict = await InboundGuard(settings, judge=llm, extractor_llm=llm).inspect(borderline)
    assert verdict is not None
    assert sorted(_prompt_names(llm)) == ["inbound_extractor.v1", "inbound_scan_judge.v1"]
    assert len(llm.calls) <= 2


@pytest.mark.parametrize(("scan_fails", "extract_fails"), [(True, False), (False, True)])
async def test_a_sub_stage_error_is_the_verdicts_error(
    settings, benign_email, monkeypatch, scan_fails, extract_fails
):
    guard = InboundGuard(settings)

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    if scan_fails:
        monkeypatch.setattr(guard.scan, "_inspect_cheap", boom)
    if extract_fails:
        monkeypatch.setattr(guard.extract, "_extract_heuristic", boom)
    verdict = await guard.inspect(benign_email)
    assert verdict is not None and verdict.error
    assert ("scan: " in verdict.error) is scan_fails
    assert ("extract: " in verdict.error) is extract_fails
    assert set(verdict.metadata["stages"]) == {"scan", "extract"}


async def test_extractor_llm_failure_is_a_fallback_not_an_error(settings, benign_email):
    llm = FakeLLMProvider(error_to_raise=LLMResponseError("down"))
    verdict = await InboundGuard(settings, extractor_llm=llm).inspect(benign_email)
    assert verdict is not None and verdict.error is None
    assert verdict.stage("extract")["metadata"]["llm_fallback"] is True


# ------------------------------------------------------------------ question sentences
@pytest.mark.parametrize(
    ("index", "expected"),
    [
        (0, ["Could you please refund the order or send a replacement?"]),
        (
            2,
            [
                "Hi, what is the warranty period for the X200 vacuum?",
                "Do I need to register it first?",
            ],
        ),
        (3, []),  # b-004: no question, no request
    ],
)
def test_question_sentences_on_fixture_emails(benign_emails, index, expected):
    assert question_sentences(benign_emails[index].text) == expected


def test_question_sentences_keep_a_two_token_question_and_requests():
    body = "Refund status?\n\nPlease resend invoice INV-1. Thanks a lot."
    assert question_sentences(body) == ["Refund status?", "Please resend invoice INV-1."]


def test_question_sentences_drop_quoted_history_and_cap_length():
    body = (
        "Where is my parcel?\n\nOn Mon, 1 Jan 2026, Shop <s@shop.example> wrote:\n"
        "> Did you get the parcel?\n> What else can we do?"
    )
    assert question_sentences(body) == ["Where is my parcel?"]
    many = " ".join(f"Question number {i}?" for i in range(20))
    assert len(question_sentences(many)) == MAX_SENTENCES
    long_q = "Could you " + " ".join(["word"] * 80) + "?"
    assert len(question_sentences(long_q)[0].split()) == 40


async def test_question_sentences_are_the_same_with_l1_on_and_off(settings, benign_emails):
    """Decision 8: the pipeline extracts them for every email, so C3-L1 does not weaken L2."""
    email = benign_emails[2]  # nothing to sanitize
    on = MailGuardPipeline(settings, GuardConfig.preset("C3"), audit=False)
    off = MailGuardPipeline(settings, GuardConfig.preset("C0"), audit=False)
    r_on = await on.inspect_inbound(email)
    r_off = await off.inspect_inbound(email)
    assert r_on.inbound_guard is not None and r_off.inbound_guard is None
    assert r_on.inbound_guard.question_sentences == r_off.metadata["question_sentences"]
    assert MailGuardPipeline.question_sentences_of(r_on) == question_sentences(email.text)
    assert MailGuardPipeline.question_sentences_of(r_off) == question_sentences(email.text)


async def test_question_sentences_use_the_sanitized_body(settings, attack_email):
    pipe = MailGuardPipeline(settings, GuardConfig.preset("C3"), audit=False)
    report = await pipe.inspect_inbound(attack_email)
    questions = MailGuardPipeline.question_sentences_of(report)
    assert "Hello, what is the warranty period for the X200?" in questions
    assert all("IGNORE" not in q for q in questions)
