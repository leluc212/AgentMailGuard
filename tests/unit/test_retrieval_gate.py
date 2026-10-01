"""scripts/retrieval_gate.py asserts what it claims: a vector-only hit on the document (3.16)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
DOC_ID = "0b3f6f0e-0000-4000-8000-000000000001"


def _load_gate() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))  # the script imports its sibling stack_smoke
    spec = importlib.util.spec_from_file_location("retrieval_gate", SCRIPTS / "retrieval_gate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _debug_body(
    *, lexical: int = 0, vector_docs: tuple[str, ...] = (DOC_ID,), degraded: bool = False
) -> dict[str, Any]:
    return {
        "explanation": {
            "retrieval_degraded": degraded,
            "vector_count": len(vector_docs),
            "lexical_count": lexical,
        },
        "constructed_query": {"query_vector_dimension": 1536},
        "vector_results": [{"document_id": d} for d in vector_docs],
    }


def _client(body: dict[str, Any], seen: list[dict[str, Any]]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {"url": str(request.url), "json": httpx.Response(200, content=request.content).json()}
        )
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_accepts_a_vector_only_hit_on_the_uploaded_document() -> None:
    seen: list[dict[str, Any]] = []
    query = {"subject": "Overdue payment", "body_text": "past due", "category": "billing"}
    async with _client(_debug_body(), seen) as http:
        await gate.check_vector_only_hit(http, uuid4(), DOC_ID, query)

    assert seen[0]["url"].endswith("/search/debug")
    assert seen[0]["json"]["subject"] == "Overdue payment"
    assert seen[0]["json"]["apply_rerank"] is False


@pytest.mark.parametrize(
    "body",
    [
        _debug_body(lexical=1),
        _debug_body(vector_docs=("another-document",)),
        _debug_body(vector_docs=()),
        _debug_body(degraded=True),
    ],
    ids=["lexical-hit", "other-document", "no-vector-hit", "degraded"],
)
async def test_rejects_anything_but_a_vector_only_hit_on_the_document(
    body: dict[str, Any],
) -> None:
    async with _client(body, []) as http:
        with pytest.raises(gate.SmokeFailure):
            await gate.check_vector_only_hit(http, uuid4(), DOC_ID, {"query": "x"})
