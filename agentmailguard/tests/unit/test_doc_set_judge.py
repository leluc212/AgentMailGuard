"""L2 Doc Scanner LLM set judge (ADR-0015 Task 10): flag-gated, monotonic, at most one call."""

from __future__ import annotations

import pytest

from mailguard.contracts.email import SourceDocument
from mailguard.contracts.verdict import ChunkVerdict, Severity
from mailguard.layers.doc_scanner import DocScanner
from mailguard.layers.doc_scanner.set_judge import (
    PROMPT_NAME,
    LLMSetJudge,
    deterministic_set_verdict,
)
from mailguard.llm.fake import FakeLLMProvider
from mailguard.llm.judge_calls import counting_guard_calls
from mailguard.llm.protocol import LLMError
from mailguard.pipeline import MailGuardPipeline
from mailguard.prompts import load_prompt
from tests.parity import BORDERLINE_CHUNK, _llm_calls

QUESTIONS = ["Could you explain what I need to do for order #55120?"]
CLEAN = "Refunds are processed within 5 business days of approval."


def _enabled(settings, on: bool = True):
    settings.doc_scanner.set_judge_llm_enabled = on
    return settings


def _cv(chunk_id: str, score: float, *, quarantined: bool = False) -> ChunkVerdict:
    return ChunkVerdict(
        chunk_id=chunk_id,
        quarantined=quarantined,
        trust="quarantined" if quarantined else "untrusted",
        severity=Severity.from_score(score, flag=0.5, block=0.7),
        score=score,
    )


def _docs(*ids: str) -> list[SourceDocument]:
    return [SourceDocument(chunk_id=i, content=f"knowledge text of {i}") for i in ids]


def _scan_docs() -> list[SourceDocument]:
    return [SourceDocument(**BORDERLINE_CHUNK), SourceDocument(chunk_id="ok", content=CLEAN)]


# --------------------------------------------------------------------------- monotonic
async def test_judge_only_adds_quarantines_and_never_lowers_severity(settings):
    verdicts = [_cv("a", 0.9, quarantined=True), _cv("b", 0.5), _cv("c", 0.1)]
    before = deterministic_set_verdict(verdicts)
    llm = FakeLLMProvider(default_response={"poisoned_chunk_ids": ["b"], "reason": "spread"})
    judge = LLMSetJudge(_enabled(settings), llm)

    after = await judge.judge(before, _docs("a", "b", "c"), verdicts, QUESTIONS)

    assert after.quarantined_chunk_ids == ["a", "b"]
    assert after.kept_chunk_ids == ["c"]
    assert after.severity.rank >= before.severity.rank and after.score >= before.score
    by_id = {c.chunk_id: c for c in after.chunks}
    assert by_id["b"].quarantined and by_id["b"].trust == "quarantined"
    assert by_id["b"].score >= settings.doc_scanner.quarantine_threshold
    assert by_id["b"].decided_by == "llm"
    assert any(f.rule_id == "llm:set_poisoned" for f in by_id["b"].findings)
    assert after.metadata["set_judge"] == "llm"
    assert after.metadata["set_judge_added"] == ["b"]
    assert after.metadata["set_judge_reason"] == "spread"
    assert after.metadata["clusters"] == before.metadata["clusters"]


async def test_judge_answer_with_no_id_keeps_every_quarantine(settings):
    verdicts = [_cv("a", 0.9, quarantined=True), _cv("b", 0.5)]
    before = deterministic_set_verdict(verdicts)
    llm = FakeLLMProvider(default_response={"poisoned_chunk_ids": [], "reason": "clean"})

    after = await LLMSetJudge(_enabled(settings), llm).judge(
        before, _docs("a", "b"), verdicts, QUESTIONS
    )

    assert after.quarantined_chunk_ids == ["a"] and after.kept_chunk_ids == ["b"]
    assert after.severity == before.severity and after.score == before.score
    assert after.metadata["set_judge"] == "llm" and after.metadata["set_judge_added"] == []


async def test_unknown_and_already_quarantined_ids_are_ignored(settings):
    verdicts = [_cv("a", 0.9, quarantined=True), _cv("b", 0.5)]
    before = deterministic_set_verdict(verdicts)
    llm = FakeLLMProvider(
        default_response={"poisoned_chunk_ids": ["ghost", "a", "<<<x"], "reason": "made up"}
    )

    after = await LLMSetJudge(_enabled(settings), llm).judge(
        before, _docs("a", "b"), verdicts, QUESTIONS
    )

    assert after.quarantined_chunk_ids == ["a"]
    assert after.kept_chunk_ids == ["b"]
    assert [c.chunk_id for c in after.chunks] == ["a", "b"]
    assert after.metadata["set_judge_added"] == []


# --------------------------------------------------------------------------- when it runs
def test_should_run_needs_flag_provider_and_an_unquarantined_chunk_in_the_band(settings):
    in_band = [_cv("b", 0.5)]
    llm = FakeLLMProvider()
    assert not LLMSetJudge(_enabled(settings, False), llm).should_run(in_band)
    assert not LLMSetJudge(_enabled(settings), None).should_run(in_band)
    judge = LLMSetJudge(_enabled(settings), llm)
    assert judge.should_run(in_band)
    assert judge.should_run([_cv("lo", 0.30)])  # band is [low, high)
    assert not judge.should_run([_cv("hi", 0.70)])
    assert not judge.should_run([_cv("clean", 0.29), _cv("q", 0.9, quarantined=True)])
    assert not judge.should_run([_cv("q", 0.5, quarantined=True)])
    assert not judge.should_run([])


async def test_flag_off_never_calls_the_provider(settings):
    llm = FakeLLMProvider(default_response={"poisoned_chunk_ids": ["kb-borderline-1"]})
    scanner = DocScanner(_enabled(settings, False), set_judge_llm=llm)

    verdict = await scanner.scan(_scan_docs(), question_sentences=QUESTIONS)

    assert llm.calls == []
    assert verdict.metadata["set_judge"] == "deterministic"
    assert verdict.kept_chunk_ids == ["kb-borderline-1", "ok"]


async def test_flag_on_without_a_chunk_in_the_band_never_calls_the_provider(settings):
    llm = FakeLLMProvider(default_response={"poisoned_chunk_ids": ["ok"]})
    scanner = DocScanner(_enabled(settings), set_judge_llm=llm)

    verdict = await scanner.scan(
        [SourceDocument(chunk_id="ok", content=CLEAN)], question_sentences=QUESTIONS
    )

    assert llm.calls == []
    assert verdict.metadata["set_judge"] == "deterministic"


async def test_flag_on_with_a_chunk_in_the_band_makes_exactly_one_call(settings):
    llm = FakeLLMProvider(
        default_response={"poisoned_chunk_ids": ["kb-borderline-1"], "reason": "coordinated"}
    )
    scanner = DocScanner(_enabled(settings), set_judge_llm=llm)

    verdict = await scanner.scan(_scan_docs(), question_sentences=QUESTIONS)

    assert len(llm.calls) == 1
    messages = llm.calls[0]["messages"]
    assert PROMPT_NAME == "doc_set_judge.v1"
    assert messages[0].role == "system" and messages[0].content == load_prompt(PROMPT_NAME)
    user = messages[-1].content
    assert QUESTIONS[0] in user
    assert user.count("<<<KNOWLEDGE:") == 2 and user.count("<<</KNOWLEDGE:") == 2
    assert BORDERLINE_CHUNK["content"] in user and CLEAN in user
    assert _llm_calls(llm) == {"doc_set_judge.v1": 1}
    assert verdict.quarantined_chunk_ids == ["kb-borderline-1"]
    assert verdict.kept_chunk_ids == ["ok"]
    assert verdict.metadata["set_judge"] == "llm"


async def test_judge_without_a_provider_returns_the_verdict_unchanged_and_counts_no_call(settings):
    verdicts = [_cv("a", 0.5), _cv("b", 0.1)]
    before = deterministic_set_verdict(verdicts)
    snapshot = before.model_dump()
    judge = LLMSetJudge(_enabled(settings), None)

    with counting_guard_calls() as calls:
        after = await judge.judge(before, _docs("a", "b"), verdicts, QUESTIONS)

    assert after is before
    assert after.model_dump() == snapshot
    assert sum(calls.values()) == 0


# --------------------------------------------------------------------------- fallback
@pytest.mark.parametrize(
    "llm",
    [
        FakeLLMProvider(default_response={"poisoned_chunk_ids": "not-a-list"}),
        FakeLLMProvider(error_to_raise=LLMError("backend down")),
    ],
    ids=["invalid-answer", "provider-error"],
)
async def test_failed_judge_falls_back_to_the_deterministic_verdict(settings, llm):
    settings.mailguard.fail_closed_on_judge_error = False  # the v2 fallback (Task 11 flag off)
    verdicts = [_cv("a", 0.9, quarantined=True), _cv("b", 0.5)]
    before = deterministic_set_verdict(verdicts)
    snapshot = before.model_dump(exclude={"metadata"})

    after = await LLMSetJudge(_enabled(settings), llm).judge(
        before, _docs("a", "b"), verdicts, QUESTIONS
    )

    assert after.model_dump(exclude={"metadata"}) == snapshot
    assert after.metadata["set_judge"] == "deterministic"
    assert after.metadata["llm_fallback"] is True
    assert after.metadata["llm_fallback_reason"]


# --------------------------------------------------------------------------- pipeline wiring
def test_pipeline_passes_doc_set_judge_to_the_doc_scanner(settings):
    llm = FakeLLMProvider()
    pipeline = MailGuardPipeline(_enabled(settings), doc_set_judge=llm, audit=False)
    assert pipeline.doc_scanner.set_judge is not None
    assert pipeline.doc_scanner.set_judge.llm is llm


def test_pipeline_builds_no_set_judge_by_default(settings):
    pipeline = MailGuardPipeline(settings, audit=False)
    assert settings.doc_scanner.set_judge_llm_enabled is False
    assert pipeline.doc_scanner.set_judge is None
