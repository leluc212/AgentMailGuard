"""Stub LLM provider for hermetic, credential-free testing (R24.5).

Simulates deterministic LLM completions and structured JSON generations
without network calls or external API keys.
"""

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class LLMGenerationResult:
    """Standard container for LLM generation responses."""

    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    parsed_json: dict[str, Any] | None = None


class StubLLMProvider:
    """Deterministic LLM Provider test double.

    Requires zero live credentials (no OpenAI, Anthropic, or Google API keys).
    Supports canned responses, structured JSON output, and failure injection.
    """

    def __init__(self, model_name: str = "stub-gpt-4o") -> None:
        self.model_name = model_name
        self.call_count = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.fail_on_next_call = False
        self.malformed_json_on_next_call = False
        self._canned_responses: list[str] = []

    def queue_canned_response(self, response: str) -> None:
        """Enqueue a specific raw text or JSON response for subsequent calls."""
        self._canned_responses.append(response)

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        max_tokens: int = 1000,
        temperature: float = 0.0,
    ) -> LLMGenerationResult:
        """Generate a deterministic completion from prompt text."""
        self.call_count += 1

        if self.fail_on_next_call:
            self.fail_on_next_call = False
            raise RuntimeError("Injected LLM provider network or timeout failure")

        if self._canned_responses:
            content = self._canned_responses.pop(0)
        elif self.malformed_json_on_next_call:
            self.malformed_json_on_next_call = False
            content = '{"category": "billing", "priority": unquoted_invalid_value}'
        else:
            # Default deterministic response
            content = f"Stub response to prompt of length {len(prompt)}: acknowledged."

        # Simulate token counts
        prompt_tokens = max(10, len(prompt) // 4)
        completion_tokens = max(5, len(content) // 4)
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens

        return LLMGenerationResult(
            content=content,
            model=self.model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )

    async def generate_structured(
        self,
        prompt: str,
        system_prompt: str | None = None,
        default_payload: dict[str, Any] | None = None,
    ) -> LLMGenerationResult:
        """Generate structured JSON response conforming to expected schema."""
        if self._canned_responses:
            res = await self.generate(prompt=prompt, system_prompt=system_prompt)
            try:
                parsed = json.loads(res.content)
            except Exception:
                parsed = None
            res.parsed_json = parsed
            return res

        payload = default_payload or {
            "category": "general_inquiry",
            "priority": "normal",
            "reply_required": True,
            "workflow_hint": "ai_generate",
            "confidence": 0.95,
            "draft": "Thank you for reaching out. We have received your request.",
        }
        json_content = json.dumps(payload)
        self.queue_canned_response(json_content)
        res = await self.generate(prompt=prompt, system_prompt=system_prompt)
        res.parsed_json = payload
        return res
