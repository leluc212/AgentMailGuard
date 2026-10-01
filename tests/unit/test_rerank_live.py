"""The real cross-encoder, end to end (R11.1, R11.5; task 7.20). Skipped unless RERANK_LIVE_TEST=1.

Every other reranker test uses a fake model. This one loads the real one, so it needs torch, the
weights and (for a model that is not cached yet) the network:

    RERANK_LIVE_TEST=1 uv run pytest tests/unit/test_rerank_live.py -q -p no:cacheprovider

The model follows RETRIEVAL__RERANK_MODEL and RETRIEVAL__RERANK_MODEL_DIR. Point the latter at the
image's /app/.cache/reranker (or a folder filled by the Dockerfile's load) to prove the baked
model loads with no network.
"""

from __future__ import annotations

import os

import pytest

from packages.core.settings import AppSettings
from packages.retrieval.models import Candidate
from packages.retrieval.rerank import CrossEncoderReranker, build_rerank_service

pytestmark = pytest.mark.skipif(
    os.environ.get("RERANK_LIVE_TEST") != "1",
    reason="loads the real cross-encoder (torch, weights, maybe network); set RERANK_LIVE_TEST=1",
)

RELEVANT = (
    "To reset your password open Settings, choose Security and press Reset password. "
    "A reset link is emailed to the address on the account."
)
UNRELATED = [
    "Our offices are closed on public holidays and support resumes the next business day.",
    "Invoices are issued on the first day of each month and are payable within 30 days.",
]


async def test_the_real_cross_encoder_ranks_the_relevant_chunk_first() -> None:
    settings = AppSettings(_env_file=None).retrieval.model_copy(
        # A live run must rerank whatever the ambient RETRIEVAL__RERANK_ENABLED says.
        update={"rerank_enabled": True, "rerank_timeout_ms": 30_000}
    )
    service = build_rerank_service(settings)
    assert service is not None and isinstance(service.reranker, CrossEncoderReranker)
    pool = [
        Candidate(chunk_id="holidays", document_id="d1", content=UNRELATED[0], fused_score=0.03),
        Candidate(chunk_id="invoices", document_id="d2", content=UNRELATED[1], fused_score=0.02),
        Candidate(chunk_id="password", document_id="d3", content=RELEVANT, fused_score=0.01),
    ]

    result = await service.rerank(
        "How do I reset my password?", pool, organization_id="org-live", top_k=2
    )

    assert result.rerank_applied and not result.fallback_recorded, result.fallback_reason
    assert [c.chunk_id for c in result.candidates][0] == "password"
    scores = [c.rerank_score for c in result.candidates if c.rerank_score is not None]
    assert len(scores) == 2, "every returned chunk carries its rerank_score"
    assert scores == sorted(scores, reverse=True)
