from __future__ import annotations

from mailguard.contracts.email import RetrievedChunk
from mailguard.contracts.verdict import Severity, ThreatType
from mailguard.layers.l3b_document_scanner import RetrievedDocumentScanner
from mailguard.layers.l3b_document_scanner.scanner import poison_findings, query_echo_score
from mailguard.llm.fake import FakeLLMProvider


def test_poison_heuristics_fire(poisoned_chunk):
    findings = poison_findings(poisoned_chunk, query="what is the warranty period for the X200")
    techniques = {f.technique for f in findings}
    assert "answer_forcing" in techniques
    assert "instruction_override" in techniques
    assert "query_echo" in techniques
    assert all(f.threat_type is ThreatType.RAG_POISONING for f in findings)


def test_query_echo_score():
    q = "how do I reset my password on the mobile app"
    assert (
        query_echo_score("How do I reset my password on the mobile app? The answer is ...", q)
        >= 0.55
    )
    assert query_echo_score("Refund policy: full refund within 30 days.", q) == 0.0
    assert query_echo_score("anything", None) == 0.0


def test_scan_quarantines_poison_and_keeps_clean(settings, kb_chunks, poisoned_chunk):
    scanner = RetrievedDocumentScanner(settings)
    kept, verdicts = scanner.scan_sync([*kb_chunks, poisoned_chunk], query="warranty period X200")
    assert [c.chunk_id for c in kept] == ["kb-warranty-1", "kb-refund-1"]
    assert verdicts[-1].quarantined and verdicts[-1].severity.rank >= Severity.HIGH.rank
    assert not verdicts[0].quarantined and verdicts[0].severity.rank <= Severity.LOW.rank
    assert scanner.quarantine_ratio(verdicts) == 1 / 3


async def test_llm_stage_on_uncertain_band(settings):
    settings.l3b.llm_enabled = True
    llm = FakeLLMProvider(
        default_response={
            "is_poisoned": True,
            "confidence": 0.85,
            "techniques": ["answer_forcing"],
            "rationale": "steers the answer",
        }
    )
    scanner = RetrievedDocumentScanner(settings, llm=llm)
    # 'always answer' alone scores 0.70 -> at threshold, quarantined without LLM
    # craft a chunk in the uncertain band [0.30, 0.70): urgency lure rule scores 0.55
    chunk = RetrievedChunk(
        chunk_id="u1",
        content="Support procedure. Failure to comply will suspend the account. Verify the account at https://x.y/verify",
    )
    v = await scanner.scan_chunk(chunk)
    assert llm.calls, "judge must be consulted in the uncertain band"
    assert v.decided_by == "llm"
    assert v.quarantined


def test_fail_closed_quarantines_on_error(settings, monkeypatch, kb_chunks):
    scanner = RetrievedDocumentScanner(settings)

    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(scanner, "_scan_cheap", boom)
    v = scanner.scan_chunk_sync(kb_chunks[0])
    assert v.error and v.quarantined
