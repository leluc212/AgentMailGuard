"""Live LLM smoke check for task 5.0: one triage call and one draft call (R14.7, R24.5).

Run on the host by hand, never in CI (reads .env like every host tool):

    make llm-smoke

It sends two real requests through the configured provider. For the Gemini API set, in .env:
LLM__PROVIDER=openai, LLM__OPENAI_API_KEY, LLM__OPENAI_BASE_URL=
https://generativelanguage.googleapis.com/v1beta/openai and the three LLM__*_MODEL names
(docs/configuration.md §2.6).

Checks, one request each:
  1. Triage: the production triage prompt and LLMTriageOutput schema, fast tier,
     max_tokens=250 (the classifier's settings). The response must parse and validate.
  2. Draft: the technical_support profile's template and reply schema, the profile's tier,
     max_tokens=1000 (the generator's settings). The response must parse and pass
     validate_draft_payload.
For each request it prints whether the response parsed and validated, the finish_reason,
the model and the token counts. It never prints the API key: the configuration line masks
it, and error text is redacted and truncated.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError
from stack_smoke import SmokeFailure

from packages.core.settings import AppSettings, LLMTiersSettings
from packages.domain.rules import EmailContext
from packages.llm.factory import create_llm_provider
from packages.llm.profile import AgentProfileRegistry
from packages.llm.protocol import (
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMResult,
    LLMSchemaValidationError,
    ModelTier,
)
from packages.llm.validation import assert_schema_matches_contract, validate_draft_payload
from services.triage_worker.llm_classifier import LLMTriageOutput, prepare_triage_prompt

TRIAGE_MAX_TOKENS = 250  # LLMTriageClassifier default (services/triage_worker/llm_classifier.py)
DRAFT_MAX_TOKENS = 1000  # SinglePassGenerator default (packages/llm/generator.py)
DRAFT_CATEGORY = "support"  # resolves to the technical_support profile
LOCAL_PROVIDERS = ("local", "ollama", "vllm")
MAX_DETAIL = 300

SENDER = "alice.smith@clientcorp.com"
SUBJECT = "Order status question"
BODY = (
    "Hi,\n\nWhat is the status of order 82915? I placed it last week and have not received "
    "a tracking update yet.\n\nThanks,\nAlice"
)
SMOKE_CHUNK_ID = "smoke-order-status-procedure"
PROCEDURE = (
    "Order status questions: look up the order, tell the customer its current status, and "
    "share the courier tracking link once the order has left the warehouse."
)


@dataclass(frozen=True)
class CallReport:
    """Outcome of one smoke request, safe to print (no key, bounded detail)."""

    name: str
    ok: bool
    detail: str
    model: str | None = None
    finish_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class UsageRecorder:
    """httpx response hook that keeps the token usage of the last completion response.

    HttpLLMProvider raises LLMSchemaValidationError before it reads `usage`, so without this
    a truncated (finish_reason=length) or unparseable response would report 0/0 tokens.
    """

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0

    async def __call__(self, response: httpx.Response) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        if response.status_code != 200:
            return
        await response.aread()  # a response hook must read the body before inspecting it
        try:
            usage = response.json().get("usage") or {}
            self.input_tokens = int(usage.get("prompt_tokens", 0))
            self.output_tokens = int(usage.get("completion_tokens", 0))
        except (ValueError, TypeError, AttributeError):
            self.input_tokens = 0
            self.output_tokens = 0


def mask_secret(value: str | None) -> str:
    """Say whether a secret is set without revealing any of it."""
    return f"set ({len(value)} chars)" if value else "unset"


def redact(text: str, secret: str | None) -> str:
    """Remove the secret, collapse whitespace and cap the length of error text."""
    if secret:
        text = text.replace(secret, "***")
    text = " ".join(text.split())
    return text if len(text) <= MAX_DETAIL else text[: MAX_DETAIL - 3] + "..."


def active_key(llm: LLMTiersSettings) -> str | None:
    """The key the configured provider sends."""
    provider = llm.provider.lower().strip()
    if provider == "anthropic":
        return llm.anthropic_api_key
    if provider in LOCAL_PROVIDERS:
        return llm.local_api_key
    return llm.openai_api_key


def describe_config(llm: LLMTiersSettings) -> list[str]:
    """Configuration lines with the key masked, plus a warning for unpriced models (R21.6)."""
    provider = llm.provider.lower().strip()
    if provider == "anthropic":
        base_url = llm.anthropic_base_url
    elif provider in LOCAL_PROVIDERS:
        base_url = llm.local_base_url
    else:
        base_url = llm.openai_base_url
    lines = [
        f"provider {provider}  base_url {base_url}  key {mask_secret(active_key(llm))}",
        f"models   fast {llm.fast_model}  strong {llm.strong_model}  fallback {llm.fallback_model}",
    ]
    unpriced = sorted({llm.fast_model, llm.strong_model, llm.fallback_model} - set(llm.price_table))
    if unpriced:
        lines.append(
            "warn     no LLM__PRICE_TABLE entry for "
            + ", ".join(unpriced)
            + ": their cost is recorded as unknown (R21.6)"
        )
    return lines


async def _call(
    provider: LLMProvider,
    *,
    name: str,
    messages: list[ChatMessage],
    schema: dict[str, Any],
    tier: ModelTier,
    max_tokens: int,
    validate: Callable[[LLMResult], str],
    secret: str | None,
    usage: UsageRecorder,
) -> CallReport:
    try:
        result = await provider.generate(
            messages=messages, schema=schema, tier=tier, max_tokens=max_tokens, temperature=0.0
        )
    except LLMSchemaValidationError as exc:
        return CallReport(
            name,
            ok=False,
            detail="response did not parse as a JSON object: " + redact(str(exc), secret),
            finish_reason=exc.finish_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
    except LLMError as exc:
        return CallReport(
            name, ok=False, detail=f"{type(exc).__name__}: {redact(str(exc), secret)}"
        )
    try:
        detail = validate(result)
    except (ValidationError, LLMSchemaValidationError) as exc:
        return CallReport(
            name,
            ok=False,
            detail="parsed, but failed validation: " + redact(str(exc), secret),
            model=result.model,
            finish_reason=result.raw_finish_reason,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
    return CallReport(
        name,
        ok=True,
        detail=detail,
        model=result.model,
        finish_reason=result.raw_finish_reason,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )


async def triage_call(
    provider: LLMProvider, secret: str | None, usage: UsageRecorder
) -> CallReport:
    """One request with the production triage prompt and schema."""
    ctx = EmailContext(sender_email=SENDER, subject=SUBJECT, body_text=BODY, body_text_clean=BODY)

    def validate(result: LLMResult) -> str:
        parsed = LLMTriageOutput.model_validate(result.content)
        return (
            f"category {parsed.category}, intent {parsed.intent}, "
            f"confidence {parsed.confidence:.2f}"
        )

    return await _call(
        provider,
        name="triage",
        messages=prepare_triage_prompt(ctx),
        schema=LLMTriageOutput.model_json_schema(),
        tier=ModelTier.FAST,
        max_tokens=TRIAGE_MAX_TOKENS,
        validate=validate,
        secret=secret,
        usage=usage,
    )


def draft_context() -> dict[str, Any]:
    """Template variables for the draft prompt: one email and one procedure chunk.

    business_data stays None so the same context renders under the v1 and v2 templates.
    """
    return {
        "current_message": {
            "sender": f"Alice Smith <{SENDER}>",
            "subject": SUBJECT,
            "received_at": "2026-09-28T09:00:00+00:00",
            "body_text": BODY,
            "body_text_clean": BODY,
        },
        "thread_summary": None,
        "recent_messages": [],
        "retrieved_chunks": [
            {"chunk_id": SMOKE_CHUNK_ID, "external_id": None, "content": PROCEDURE}
        ],
        "business_data": None,
    }


async def draft_call(
    provider: LLMProvider,
    registry: AgentProfileRegistry,
    secret: str | None,
    usage: UsageRecorder,
) -> CallReport:
    """One request with a real profile's template and reply schema."""
    profile = registry.resolve_profile(DRAFT_CATEGORY)
    schema = registry.get_schema(profile)
    assert_schema_matches_contract(schema)
    prompt = registry.render_prompt(profile, draft_context())

    def validate(result: LLMResult) -> str:
        payload = validate_draft_payload(result.content, schema)
        cited = "yes" if SMOKE_CHUNK_ID in payload.knowledge_chunks else "no"
        return (
            f"profile {profile.profile} ({profile.prompt_version}), action {payload.action}, "
            f"cites the given chunk: {cited}"
        )

    return await _call(
        provider,
        name="draft",
        messages=[ChatMessage(role="user", content=prompt)],
        schema=schema,
        tier=ModelTier(profile.model_tier),
        max_tokens=DRAFT_MAX_TOKENS,
        validate=validate,
        secret=secret,
        usage=usage,
    )


async def run(settings: AppSettings, client: httpx.AsyncClient) -> list[CallReport]:
    """Send the triage and the draft request through the configured provider."""
    llm = settings.llm
    if llm.provider.lower().strip() == "fake":
        raise SmokeFailure(
            "LLM__PROVIDER is fake; set LLM__PROVIDER=openai with the Gemini base URL, key and "
            "models in .env (docs/configuration.md §2.6)"
        )
    secret = active_key(llm)
    provider = create_llm_provider(llm, client=client)
    registry = AgentProfileRegistry.from_yaml(settings.agent_profiles.config_path)
    usage = UsageRecorder()
    client.event_hooks["response"].append(usage)
    try:
        return [
            await triage_call(provider, secret, usage),
            await draft_call(provider, registry, secret, usage),
        ]
    finally:
        client.event_hooks["response"].remove(usage)


def render_reports(reports: list[CallReport]) -> list[str]:
    """Two printable lines per request."""
    lines: list[str] = []
    for report in reports:
        mark = "ok  " if report.ok else "FAIL"
        lines.append(f"{mark} {report.name}: {report.detail}")
        lines.append(
            f"     model {report.model or '-'}  finish_reason {report.finish_reason or '-'}  "
            f"tokens in {report.input_tokens} out {report.output_tokens}"
        )
    return lines


async def _run_with_client(settings: AppSettings) -> list[CallReport]:
    async with httpx.AsyncClient(timeout=settings.llm.timeout_s) as client:
        return await run(settings, client)


def main() -> int:
    # The HTTP client logs provider error bodies; keep output to the redacted lines below.
    logging.getLogger("packages.llm").setLevel(logging.CRITICAL)
    try:
        settings = AppSettings()
    except ValidationError as exc:
        # Print locations and messages only: input values could include the key.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        print(f"FAIL settings: {problems}", file=sys.stderr)
        return 1
    for line in describe_config(settings.llm):
        print(line)
    try:
        reports = asyncio.run(_run_with_client(settings))
    except SmokeFailure as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    for line in render_reports(reports):
        print(line)
    if not all(report.ok for report in reports):
        print("FAIL at least one request did not return a schema-valid response", file=sys.stderr)
        return 1
    print("LLM SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
