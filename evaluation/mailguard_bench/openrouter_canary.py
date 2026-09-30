"""A few-cent canary before an OpenRouter benchmark run (work package R4; owner-run, never CI).

    uv run python -m evaluation.mailguard_bench.openrouter_canary --model-profile P

where P is ``llama-3.1-8b-openrouter`` or ``qwen2.5-7b-openrouter``.

One strict-JSON call, through rag-email's own pinned client (``packages/llm``), to the model the
profile pins. It answers three questions before a long run spends money:

- ``strict_json``: does the pinned provider return an answer that is valid for the strict JSON
  schema (OpenRouter says enforcement varies by provider, so every answer is validated here);
- ``provider_match``: was the call served by the pinned provider on the first attempt (the
  client's own check, the one every benchmark call goes through);
- ``captured``: the exact request and response are written to
  ``<out>/<profile>.json`` (the key is never written), so the owner can read what OpenRouter
  really sends back, ``openrouter_metadata`` and the generation id included.

It makes exactly one call, about 40 tokens: under a cent at these prices. It exits 1 when a check
fails and says why. The guard's judges have their own pinned check: ``make mailguard-probe
MODEL=<profile>`` (``guard_smoke``) makes one guard-judge call and checks the same pin.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from evaluation.mailguard_bench.guard_env import REPO_ROOT
from evaluation.mailguard_bench.model_profiles import (
    PROFILES,
    ModelProfile,
    ModelProfileError,
    get_profile,
    profile_env,
    with_dot_env,
)
from packages.core.settings import AppSettings, LLMTiersSettings
from packages.llm.factory import create_llm_provider
from packages.llm.protocol import (
    CallProvenance,
    ChatMessage,
    LLMError,
    LLMProviderMismatchError,
    LLMResponseError,
    LLMSchemaValidationError,
    ModelTier,
)

DEFAULT_OUT = REPO_ROOT / "evaluation" / "results" / "mailguard_bench" / "canary"
CANARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["pong"]}},
    "required": ["answer"],
    "additionalProperties": False,
}
CANARY_MAX_TOKENS = 40
ROUTED_PROFILES = sorted(name for name, profile in PROFILES.items() if profile.routing is not None)


@dataclass
class CanaryReport:
    """What one canary call showed."""

    profile: str
    model: str
    checks: dict[str, bool]
    problems: list[str] = field(default_factory=list)
    served_provider: str | None = None
    provider_source: str | None = None
    generation_id: str | None = None
    cost_usd: float | None = None
    path: Path | None = None

    @property
    def ok(self) -> bool:
        return all(self.checks.values()) and not self.problems


class _CapturingTransport(httpx.AsyncBaseTransport):
    """Passes each request to ``inner`` and keeps the exchange, without the Authorization header."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.request: dict[str, Any] | None = None
        self.response: dict[str, Any] | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request = {
            "method": request.method,
            "url": str(request.url),
            "headers": {k: v for k, v in request.headers.items() if k.lower() != "authorization"},
            "body": _json_or_text(request.content),
        }
        response = await self.inner.handle_async_request(request)
        content = await response.aread()
        self.response = {
            "status": response.status_code,
            "headers": dict(response.headers),
            "body": _json_or_text(content),
        }
        return httpx.Response(
            response.status_code, headers=response.headers, content=content, request=request
        )

    async def aclose(self) -> None:
        await self.inner.aclose()


def _json_or_text(content: bytes) -> Any:
    text = content.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _hint(exc: LLMResponseError) -> str:
    """What an HTTP failure means for this route, in the operator's words."""
    status = exc.status_code
    if status == 402:
        if exc.limit_source == "openrouter_in_flight_budget":
            return "HTTP 402, the in-flight budget: transient, retry after a few seconds"
        return (
            "HTTP 402, no credit or the key's limit is used up "
            f"({exc.limit_source or 'no limit_source'}): fund the account, then run again"
        )
    if status == 401:
        return "HTTP 401: OpenRouter rejected the key (BENCH_OPENROUTER_API_KEY)"
    if status in (404, 502, 503):
        return (
            f"HTTP {status}: no provider serves the pinned route (fallbacks are off): "
            "check the provider is still listed for this model"
        )
    return str(exc)[:300]


def _answer_is_valid(content: dict[str, Any]) -> bool:
    return set(content) == {"answer"} and content["answer"] == "pong"


async def run_canary(
    profile: ModelProfile,
    llm: LLMTiersSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    out_dir: Path = DEFAULT_OUT,
) -> CanaryReport:
    """One strict-JSON call to the profile's pinned route; the exchange is written to ``out_dir``.

    ``transport`` replaces the network (tests); the default is the real HTTP transport.
    """
    capture = _CapturingTransport(transport or httpx.AsyncHTTPTransport())
    client = httpx.AsyncClient(transport=capture, timeout=llm.timeout_s)
    provider = create_llm_provider(llm, client=client)
    report = CanaryReport(
        profile=profile.name,
        model=profile.model,
        checks={"strict_json": False, "provider_match": False, "captured": False},
    )
    provenance: CallProvenance | None = None
    try:
        result = await provider.generate(
            messages=[
                ChatMessage(role="system", content="You answer only with a JSON object."),
                ChatMessage(role="user", content='Return exactly {"answer": "pong"}.'),
            ],
            schema=CANARY_SCHEMA,
            tier=ModelTier.FAST,
            max_tokens=CANARY_MAX_TOKENS,
        )
        provenance = result.provenance
        report.checks["provider_match"] = True  # the client raises on any other provider
        if _answer_is_valid(result.content):
            report.checks["strict_json"] = True
        else:
            report.problems.append(
                f"the answer is not valid for the strict schema: {result.content}"
            )
    except LLMProviderMismatchError as exc:
        provenance = exc.provenance
        report.problems.append(str(exc))
    except LLMSchemaValidationError as exc:
        provenance = exc.provenance
        report.checks["provider_match"] = provenance is not None  # checked before the parse
        report.problems.append(f"the answer is not schema-valid JSON: {str(exc)[:300]}")
    except LLMResponseError as exc:
        report.problems.append(_hint(exc))
    except LLMError as exc:
        report.problems.append(f"{type(exc).__name__}: {str(exc)[:300]}")
    finally:
        await client.aclose()
    if provenance is not None:
        report.served_provider = provenance.served_provider
        report.provider_source = provenance.provider_source
        report.generation_id = provenance.generation_id
        report.cost_usd = provenance.cost
    if capture.response is not None:
        report.checks["captured"] = True
        out_dir.mkdir(parents=True, exist_ok=True)
        report.path = out_dir / f"{profile.name}.json"
        report.path.write_text(
            json.dumps(
                {
                    "profile": profile.name,
                    "model": profile.model,
                    "captured_at": datetime.now(UTC).isoformat(),
                    "request": capture.request,
                    "response": capture.response,
                    "provenance": provenance.to_dict() if provenance is not None else None,
                    "checks": report.checks,
                    "problems": report.problems,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    return report


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--model-profile", required=True, choices=ROUTED_PROFILES)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where the capture goes")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        profile = get_profile(args.model_profile)
        os.environ.update(profile_env(profile, with_dot_env(os.environ, args.env_file)))
    except ModelProfileError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    llm = AppSettings().llm
    report = asyncio.run(run_canary(profile, llm, out_dir=args.out))
    served = report.served_provider or "unknown"
    cost = "n/a" if report.cost_usd is None else f"${report.cost_usd:.7f}"
    for name, passed in report.checks.items():
        print(f"{'ok  ' if passed else 'FAIL'} {name}")
    for problem in report.problems:
        print(f"FAIL {problem}", file=sys.stderr)
    source = report.provider_source or "no field"
    print(
        f"{profile.name}: served by {served} (read from {source}), cost {cost}, "
        f"generation {report.generation_id}"
    )
    if report.path is not None:
        print(f"captured request and response (no key) in {report.path}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
