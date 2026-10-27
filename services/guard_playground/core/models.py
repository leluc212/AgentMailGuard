"""The 3 Draft_Models of Live_Mode (design.md "core/models.py"; Requirements 7.14-7.16, D9).

Every judge of the guard runs on the selected Draft_Model and its route, as the 2026-10-19
benchmark runs did (D9). Routing objects are copied from
``evaluation/mailguard_bench/guard_models.yaml``; a unit test checks they still match.

API keys are read from the environment at call time and never stored on a rendered object.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
JUDGES: tuple[str, ...] = (
    "inbound_judge",
    "inbound_extractor",
    "doc_judge",
    "doc_set_judge",
    "output_judge",
)


@dataclass(frozen=True)
class DraftModel:
    key: str  # "gpt-4o-mini" | "qwen" | "llama" (Replay file names use it)
    label: str
    model_id: str
    route_label: str
    base_url: str
    key_env: tuple[str, ...]  # env var names, first non-empty wins
    timeout_s: float
    provider_routing: Mapping[str, Any] | None = field(default=None)

    @property
    def response_metadata(self) -> bool:
        return self.provider_routing is not None


DRAFT_MODELS: tuple[DraftModel, ...] = (
    DraftModel(
        key="gpt-4o-mini",
        label="gpt-4o-mini",
        model_id="gpt-4o-mini",
        route_label="OpenAI API",
        base_url=OPENAI_BASE_URL,
        key_env=("OPENAI_API_KEY", "BENCH_OPENAI_API_KEY"),
        timeout_s=60.0,
    ),
    DraftModel(
        key="qwen",
        label="Qwen2.5-7B-Instruct",
        model_id="qwen/qwen-2.5-7b-instruct",
        route_label="OpenRouter, Phala",
        base_url=OPENROUTER_BASE_URL,
        key_env=("BENCH_OPENROUTER_API_KEY",),
        timeout_s=120.0,
        provider_routing={"order": ["phala"], "allow_fallbacks": False, "require_parameters": True},
    ),
    DraftModel(
        key="llama",
        label="Llama-3.1-8B-Instruct",
        model_id="meta-llama/llama-3.1-8b-instruct",
        route_label="OpenRouter, CoreWeave, bf16",
        base_url=OPENROUTER_BASE_URL,
        key_env=("BENCH_OPENROUTER_API_KEY",),
        timeout_s=120.0,
        provider_routing={
            "order": ["coreweave"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "quantizations": ["bf16"],
        },
    ),
)
MODELS_BY_KEY: dict[str, DraftModel] = {m.key: m for m in DRAFT_MODELS}


def get_model(key: str) -> DraftModel:
    try:
        return MODELS_BY_KEY[key]
    except KeyError:
        raise ValueError(f"unknown Draft_Model {key!r}") from None


def model_choices() -> list[tuple[str, str, str]]:
    """(key, label, route_label) in the fixed order of Requirement 7.14."""
    return [(m.key, m.label, m.route_label) for m in DRAFT_MODELS]


def _api_key(m: DraftModel, environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    for name in m.key_env:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def credential_present(m: DraftModel, environ: Mapping[str, str] | None = None) -> bool:
    """True when the model's API key is configured (the value is never returned)."""
    return bool(_api_key(m, environ))


def missing_credential_message(m: DraftModel) -> str:
    """Requirement 7.16: names the model and the variable, never a value."""
    return f"{m.label} needs an API key: set {' or '.join(m.key_env)} and restart the Playground"


def make_provider(m: DraftModel) -> Any:
    """One guard ``OpenAIProvider`` on the model's route (imports the guard lazily)."""
    from mailguard.llm.openai_provider import OpenAIProvider

    return OpenAIProvider(
        model=m.model_id,
        base_url=m.base_url,
        api_key=_api_key(m),
        timeout_s=m.timeout_s,
        json_mode="json_object",
        provider_routing=m.provider_routing,
        response_metadata=m.response_metadata,
    )


def guard_providers(m: DraftModel) -> dict[str, Any]:
    """Keyword arguments for ``MailGuardPipeline``: all 5 judges on the Draft_Model (D9)."""
    provider = make_provider(m)
    return dict.fromkeys(JUDGES, provider)


DraftAgent = Callable[[list[Any]], Awaitable[dict[str, Any]]]

REPLY_INSTRUCTION = (
    'Answer with one JSON object: {"draft": "<the reply email body>", '
    '"knowledge_chunks": ["<citation ids you used>"]}.'
)


def draft_agent(m: DraftModel, *, max_tokens: int = 800) -> DraftAgent:
    """The Draft_Agent: sends the guard-built messages to the model, returns the reply JSON."""
    provider = make_provider(m)

    async def generate(messages: list[Any]) -> dict[str, Any]:
        from mailguard.llm.protocol import ChatMessage

        chat = [ChatMessage(role=str(x.role), content=str(x.content)) for x in messages]
        chat.append(ChatMessage(role="system", content=REPLY_INSTRUCTION))
        result = await provider.generate(
            messages=chat, schema={"type": "object"}, max_tokens=max_tokens, temperature=0.0
        )
        content = dict(result.content)
        if "draft" not in content and "raw_text" in content:
            content = {"draft": str(content["raw_text"]), "knowledge_chunks": []}
        return content

    return generate
