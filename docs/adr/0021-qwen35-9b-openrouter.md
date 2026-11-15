# ADR-0021: Qwen3.5-9B on OpenRouter (PrimeIntellect bf16) replaces Qwen2.5-7B in the v4 benchmark

- **Status:** Accepted, 2026-11-15
- **Date:** 2026-11-15
- **Decided by:** project owner (chat, 2026-11-15: run the benchmark with `qwen/qwen3.5-9b` on
  OpenRouter instead of Qwen2.5; "Pin PrimeIntellect bf16, thay Qwen2.5 bằng Qwen3.5, đồng ý chuẩn
  bị + chạy thử")
- **Task:** `specs/tasks.md` 7.36
- **Amends:** ADR-0020 for the paper: the Qwen model of the v4 results is Qwen3.5-9B. The
  Qwen2.5-7B local run (`2026-11-11-qwen25-local-v4`) stays in the repository and is not reported.
  gpt-4o-mini and Llama-3.1-8B keep their ADR-0014 routes.
- **Changes:** a model profile `qwen3.5-9b-openrouter` (`evaluation/mailguard_bench/model_profiles.py`),
  its guard entry (`evaluation/mailguard_bench/guard_models.yaml`), a reasoning-token check in the
  canary (`openrouter_canary.py`), tests. No change to `agentmailguard/`, `packages/` or scoring.

## Context

OpenRouter no longer serves `qwen/qwen-2.5-7b-instruct` (ADR-0020). The owner chose
`qwen/qwen3.5-9b` (Qwen3.5 family, 9B, released 2026-03-10, text+image+video input) on OpenRouter.
It is another model, not a host for Qwen2.5-7B: its results are not comparable with the Qwen2.5 runs.

OpenRouter's live API (`/api/v1/models/qwen/qwen3.5-9b/endpoints`, read 2026-11-15) lists 8
endpoints. All list `response_format` and `structured_outputs`.

| Provider | Precision | $/1M in / out | Uptime 1 d |
|---|---|---|---|
| PrimeIntellect | bf16 | 0.07 / 0.103 | 100 % |
| DeepInfra (flex, standard, priority) | bf16 | 0.08–0.15 / 0.12–0.225 | 100 % |
| Darkbloom | fp4 | 0.08 / 0.13 | 100 % |
| SiliconFlow | fp8 | 0.10 / 0.15 | 75 % |
| Parasail | bf16 | 0.10 / 0.25 | 36 % |
| Together | unknown | 0.17 / 0.25 | 96 % |

**Qwen3.5 thinks by default on some endpoints.** Probe calls (2026-11-15, strict `json_schema` and
`json_object`, `max_tokens` 400, no `reasoning` field, `.cache/phaseE/probe_qwen35*.py`):

- PrimeIntellect bf16: 0 reasoning tokens, schema-valid JSON in 11 of 11 calls, 1.2–3.2 s.
- DeepInfra bf16: 389 and 424 reasoning tokens, empty content, `finish_reason` `length`.
- Together: 400 reasoning tokens, content "Thinking Process: …", `finish_reason` `length`.
- With `"reasoning": {"enabled": false}` (or `effort: none`) all three answer schema-valid JSON.

Neither client can send that field: rag-email's `packages/llm` sends only the provider object, and
the guard's provider (pinned `d95e542`, not edited) sends no extra body field.

## Decision

1. **Profile `qwen3.5-9b-openrouter`:** model `qwen/qwen3.5-9b`, pinned to `primeintellect` with
   `quantizations: [bf16]`, fallbacks off, `require_parameters` on, the same object in
   `guard_models.yaml` (a test checks they agree). Prices 0.07 / 0.103 per 1M tokens.
2. **The pin is chosen for reasoning off, not price.** No code change switches reasoning off.
3. **The canary fails on any reasoning token** (`usage.completion_tokens_details.reasoning_tokens`
   > 0), so a provider that starts thinking stops the run before it spends money. The run records the
   served provider of every call (provenance), as for Llama.
4. **Everything else is unchanged:** case set `mailguard_v4`, classifier `9c3c6c46…`, guard pin
   `d95e542`, embedding `text-embedding-3-small`, configs C0 and C3, scoring, concurrency 2, LLM
   timeout 60 s and case budget 300 s (the hosted runs' values; the 180 s of ADR-0020 was local).
5. **Trial first:** canary, guard probe, then a C3 trial on 10 heavy cases (RUN `trial-qwen35`,
   never a result) before the run.

## Consequences

- The paper's third model is Qwen3.5-9B (bf16, hosted, reasoning off), not Qwen2.5-7B. It is a newer
  and larger model; a difference with gpt-4o-mini and Llama-3.1-8B is not a Qwen2.5 result.
- One provider, no fallback: a PrimeIntellect outage or rate limit fails calls (one HTTP 429
  "temporarily rate-limited upstream" was seen on 2026-11-15; 40 of 40 calls at concurrency 2 passed
  a minute later). Failed cases are retried by the kit's retry pass; those still failing are left
  out of n, as for the other models.
- If PrimeIntellect changes its default to reasoning, the canary fails and the owner decides again;
  rows already written record their provider.
- Cost: lower per token than Llama-3.1-8B (0.22 / 0.22).
