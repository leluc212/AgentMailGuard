"""Retrieval and hybrid search package.

Requirements:
- R10.7: SearchBackend interface abstraction.
- R10.8: Candidate models carrying both ranks and both scores.
- Task 3.7: SearchBackend interface and contract test suite.
"""

from packages.retrieval.fake import FakeSearchBackend
from packages.retrieval.models import Candidate, RetrievalQuery
from packages.retrieval.protocol import SearchBackend
from packages.retrieval.testing import SearchBackendContractSuite

__all__ = [
    "Candidate",
    "FakeSearchBackend",
    "RetrievalQuery",
    "SearchBackend",
    "SearchBackendContractSuite",
]
