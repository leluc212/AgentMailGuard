"""Retrieval and hybrid search package.

Requirements:
- R10.7: SearchBackend interface abstraction.
- R10.8: Candidate models carrying both ranks and both scores.
- R10.1: PostgresSearchBackend implementation.
- Task 3.7 & 3.8: SearchBackend interface and PostgreSQL implementation.
"""

from packages.retrieval.fake import FakeSearchBackend
from packages.retrieval.models import Candidate, RetrievalQuery
from packages.retrieval.postgres import PostgresSearchBackend
from packages.retrieval.protocol import SearchBackend
from packages.retrieval.rerank import (
    CrossEncoderReranker,
    Reranker,
    RerankerUnavailableError,
    RerankPolicy,
    RerankResult,
    RerankService,
    StubReranker,
)
from packages.retrieval.retriever import HybridRetriever, RetrievalError, RetrievalResult
from packages.retrieval.rrf import (
    DEFAULT_RRF_K,
    compute_rrf_score,
    fuse_lexical_and_vector,
    reciprocal_rank_fusion,
)
from packages.retrieval.testing import SearchBackendContractSuite

__all__ = [
    "Candidate",
    "CrossEncoderReranker",
    "DEFAULT_RRF_K",
    "FakeSearchBackend",
    "HybridRetriever",
    "PostgresSearchBackend",
    "RerankPolicy",
    "RerankResult",
    "RerankService",
    "Reranker",
    "RerankerUnavailableError",
    "RetrievalError",
    "RetrievalQuery",
    "RetrievalResult",
    "SearchBackend",
    "SearchBackendContractSuite",
    "StubReranker",
    "compute_rrf_score",
    "fuse_lexical_and_vector",
    "reciprocal_rank_fusion",
]

