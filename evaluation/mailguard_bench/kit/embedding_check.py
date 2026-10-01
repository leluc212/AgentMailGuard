"""One embedding call before a model run spends anything (task 7.29; ADR-0014, owner decision
2026-10-01).

    make bench-run ─ stack env rendered (settings checked by name) ─▶ THIS: one embedding call
                   ─▶ the stack up ─▶ the configs (the model calls, the knowledge uploads)

The doctor and the stack env check the runner's ``EMBEDDING__*`` settings by name only, and the
first document the run embeds belongs to its first RAG case, hours in. So ``make bench-run`` makes
one embedding call first, through the code path the services embed with
(``packages.knowledge.embedder.get_embedder``, the live ``HttpEmbedder``), with the settings the
host processes read (the shell over ``.env``), and refuses to start unless it returns one vector of
1536 numbers, the width of the knowledge vector column. That catches, before any model call:

- an endpoint that ignores ``dimensions`` and returns its own width (``Returned embedding
  dimension N``), or one that rejects the parameter (HTTP 400; OpenAI ``text-embedding-ada-002``);
- a wrong key (401/403), model or URL (404), an unreachable endpoint;
- a quota, balance, spend limit or daily cap already used up (``quota_exhausted``), so a resume
  after a limit stop does not start before the limit is lifted.

Exactly one HTTP request: the embedder's own retries are turned off for it. The call costs a few
tokens at the endpoint's price. It runs on the runner's machine with the runner's key; the doctor
stays free of calls, and tests pass a fake transport.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from evaluation.mailguard_bench.live.stack_env import EMBEDDING_DIMENSION
from packages.core.settings import EmbeddingSettings
from packages.knowledge.embedder import (
    EmbeddingDimensionMismatchError,
    EmbeddingError,
    EmbeddingQuotaExhaustedError,
    EmbeddingRateLimitError,
    get_embedder,
)

PROBE_TEXT = "rag-email benchmark: one embedding before the run"
"""What the check embeds: a fixed line, never a case's text."""
PREFIX = "EMBEDDING__"


class EmbeddingCheckError(Exception):
    """The embedding cannot serve the run; the text says why and what to fix (never a key)."""


@dataclass(frozen=True)
class EmbeddingCheck:
    """What the one call showed: the model, its endpoint's host and the width it returned."""

    model: str
    host: str | None
    dimension: int


def embedding_settings(environ: Mapping[str, str]) -> EmbeddingSettings:
    """The runner's embedding settings as the host processes read them, for ONE request.

    ``environ`` is the shell over ``.env`` (``with_dot_env``); blank counts as unset, as the stack
    env counts it. The embedder's retries are turned off: the check is one request.

    Raises:
        EmbeddingCheckError: If a setting does not parse (named, its value never repeated).
    """
    values = {
        name.removeprefix(PREFIX).lower(): value.strip()
        for name, value in environ.items()
        if name.startswith(PREFIX) and value.strip()
    }
    try:
        settings = EmbeddingSettings.model_validate(values)
    except ValidationError as exc:
        names = sorted({PREFIX + str(err["loc"][0]).upper() for err in exc.errors() if err["loc"]})
        raise EmbeddingCheckError(
            f"the embedding settings do not parse: {', '.join(names) or 'EMBEDDING__*'}"
        ) from exc
    return settings.model_copy(update={"max_retries": 0})


def _refusal(exc: EmbeddingError, settings: EmbeddingSettings) -> EmbeddingCheckError:
    """What a failed call means for the run, in the runner's words."""
    where = f"{settings.model_name} at {urlsplit(settings.base_url).hostname}"
    if isinstance(exc, EmbeddingDimensionMismatchError):
        return EmbeddingCheckError(
            f"{where} did not return {EMBEDDING_DIMENSION}-dimension vectors ({exc}): the "
            "endpoint ignores the `dimensions` parameter for this model. Choose a model that "
            f"returns {EMBEDDING_DIMENSION} when asked (OpenAI text-embedding-3-small, or Gemini "
            "gemini-embedding-001 at 1536; docs/BENCHMARK.md part C)"
        )
    if isinstance(exc, EmbeddingQuotaExhaustedError):
        return EmbeddingCheckError(
            f"{where}: {exc}. The embedding provider's quota, balance, spend limit or daily cap "
            "is used up; restore it (a daily cap resets the next day), then run the same "
            "command again"
        )
    text = str(exc)
    hint = ""
    if isinstance(exc, EmbeddingRateLimitError):
        hint = ": a rate limit (HTTP 429); wait a minute, then run the same command again"
    elif "HTTP 400" in text:
        hint = (
            ": the endpoint refused the request; a model that rejects the `dimensions` parameter "
            "(OpenAI text-embedding-ada-002) answers 400. Choose another model (part C)"
        )
    elif "HTTP 401" in text or "HTTP 403" in text:
        hint = ": the endpoint refused the key (EMBEDDING__API_KEY)"
    elif "HTTP 404" in text:
        hint = ": no such model or URL (EMBEDDING__MODEL_NAME, EMBEDDING__BASE_URL)"
    return EmbeddingCheckError(f"the embedding call to {where} failed: {text}{hint}")


async def probe_embedding(
    settings: EmbeddingSettings, *, transport: httpx.AsyncBaseTransport | None = None
) -> EmbeddingCheck:
    """Embed ``PROBE_TEXT`` once through ``get_embedder`` and check the vector's width.

    ``transport`` replaces the network (tests); the default is the real HTTP transport.

    Raises:
        EmbeddingCheckError: If the settings select the fake embedder, the call fails, or the
            answer is not one vector of ``EMBEDDING_DIMENSION`` numbers.
    """
    if settings.mock:
        raise EmbeddingCheckError(
            "EMBEDDING__MOCK is true: the benchmark never embeds with the fake embedder"
        )
    if settings.dimension != EMBEDDING_DIMENSION:
        raise EmbeddingCheckError(
            f"EMBEDDING__DIMENSION is {settings.dimension}, expected {EMBEDDING_DIMENSION}"
        )
    client = (
        None
        if transport is None
        else httpx.AsyncClient(transport=transport, timeout=settings.timeout_s)
    )
    embedder = get_embedder(settings, client=client)
    try:
        result = await embedder.embed_texts([PROBE_TEXT])
    except EmbeddingError as exc:
        raise _refusal(exc, settings) from exc
    finally:
        if client is not None:
            await client.aclose()
        aclose = getattr(embedder, "aclose", None)
        if aclose is not None:
            await aclose()
    vectors = result.embeddings
    if len(vectors) != 1 or len(vectors[0]) != EMBEDDING_DIMENSION:
        widths = [len(vector) for vector in vectors]
        raise EmbeddingCheckError(
            f"{settings.model_name} answered {len(vectors)} vector(s) of width {widths}, "
            f"expected one of {EMBEDDING_DIMENSION}"
        )
    return EmbeddingCheck(
        model=settings.model_name,
        host=urlsplit(settings.base_url).hostname,
        dimension=len(vectors[0]),
    )


def check_embedding(
    environ: Mapping[str, str], *, transport: httpx.AsyncBaseTransport | None = None
) -> EmbeddingCheck:
    """``probe_embedding`` with the runner's settings (the shell over ``.env``).

    Raises:
        EmbeddingCheckError: If the embedding cannot serve the run.
    """
    return asyncio.run(probe_embedding(embedding_settings(environ), transport=transport))
