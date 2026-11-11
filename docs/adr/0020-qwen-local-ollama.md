# ADR-0020: Qwen2.5-7B runs locally on Ollama (Q4_K_M) in the v4 benchmark

- **Status:** Accepted, 2026-11-11
- **Date:** 2026-11-11
- **Decided by:** project owner (chat, 2026-11-11: "tôi ưu tiên chạy đúng model nên vì vậy tôi sẽ chạy
  ollama local"; "chạy Qwen local sau Llama luôn, tôi chấp nhận điều đó")
- **Task:** `specs/tasks.md` 7.36
- **Amends:** ADR-0014 for Qwen2.5-7B only (its OpenRouter route pinned to Phala). gpt-4o-mini and
  Llama-3.1-8B keep their ADR-0014 routes.
- **Changes:** which model profile the Qwen run uses (`qwen2.5-7b`, already in
  `evaluation/mailguard_bench/model_profiles.py`), `.env` (`OLLAMA_KEEP_ALIVE`), the paper notes.
  No code, no guard change.

## Context

On 2026-11-11 the Qwen run could not start on its ADR-0014 route:

- The OpenRouter canary got HTTP 404: "qwen/qwen-2.5-7b-instruct is no longer available on
  OpenRouter: no provider serves this model anymore" (`openrouter_metadata.endpoints.total` 0;
  capture in `evaluation/results/mailguard_bench/canary/qwen2.5-7b-openrouter.json`).
- OpenRouter's public API, checked twice that day: `/api/v1/models/qwen/qwen-2.5-7b-instruct/endpoints`
  lists 0 endpoints and `/api/v1/models` (469 models) no longer lists the slug. The Qwen models it
  still serves are 27B or larger.
- Other hosted routes found (Featherless, Eden AI, Together's "Turbo", Alibaba Model Studio) could
  not be confirmed to serve the official `Qwen/Qwen2.5-7B-Instruct` weights with strict JSON-schema
  output; none was tried.

The owner chose the exact model over a hosted route.

## Decision

1. **Profile `qwen2.5-7b`:** Ollama `qwen2.5:7b-instruct` (Q4_K_M, 4.4 GB, digest
   `845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e`, Ollama 0.40.16) on the owner's laptop
   (RTX 4050 Laptop, 6 GB; 3.9 GB of the 4.8 GB loaded model on the GPU, the rest on the CPU),
   reached by the containers through `host.docker.internal`. No API key, no provider pin.
2. **Context length 8,192.** The longest case email is 6,103 characters and the longest RAG case
   holds 6,700 characters of chunks; with the system and guard prompts a call can pass Ollama's
   default 4,096. The run records the loaded model's context length in its fingerprint.
3. **Concurrency 1.** One GPU that does not hold the whole model; two calls in flight only queue.
4. **LLM timeout 180 s for this run** (`LLM__TIMEOUT_S`; gpt-4o-mini and Llama ran with the
   benchmark's 60 s, `stack_env.DEFAULT_LLM_TIMEOUT_S`). The spec sets no LLM-call timeout (NFR10/11
   are end-to-end targets, not a call budget; R13.7 is business data). Measured on this laptop
   (`.cache/phaseC/ollama_long.log`, 2026-11-11), at the prompt sizes of the gpt-4o-mini C3 rows:
   a median draft (1,180 prompt / 151 output tokens) 9.5 s, p95 (2,688 / 316) 21.9 s, the largest
   seen (5,028 / 325) 27.0 s, at 13–19 output tokens/s. The triage, ai and guard workers share one
   Ollama that answers one call at a time, so two long calls in a row reach about 55 s, and a draft
   at its full 1,000-token budget about 75 s. With 60 s such calls would be error rows left out of n
   for the machine's speed, not the model's answer. The timeout changes no prompt, answer or score;
   the run records it (`generation.timeout_s`). Owner decision, 2026-11-11. Passed as
   `kit.campaign run --llm-timeout-s 180` (commit `4c4b964`), which also checks `.env` against it.
5. **Case budget unchanged: 300 s** (`live.run` default, the same as gpt-4o-mini and Llama).
   Measured on a C3 trial of the 10 heaviest cases (RUN `trial-qwen-local-timing`, never a result:
   6 PoisonedRAG / phase-1 cases with 4–8 guard calls, 2 seed RAG, the longest email attack and
   the longest benign email, picked from the Llama C3 rows): all 10 `ok` on the first attempt,
   slowest 111.3 s (61 s of it the context step, KB ingestion while Ollama was busy), the others
   7–59 s; 0 timeouts, 0 `not recognized`, 0 `Unterminated string` in its logs; 6.6 min for the 10.
   300 s is about 2.7 times the slowest, so no per-run case budget is needed. Owner decision,
   2026-11-11.
6. **Runs after Llama-3.1-8B**, on the same stack, never alongside it.
7. **Everything else is unchanged:** case set `mailguard_v4`, classifier `9c3c6c46…`, guard pin
   `d95e542`, embedding `text-embedding-3-small` (still OpenAI), configs C0 and C3, scoring.

## Consequences

- The Qwen results are **Qwen2.5-7B-Instruct, Q4_K_M, local**, not the bf16/full-precision hosted
  model of gpt-4o-mini and Llama-3.1-8B. A difference between Qwen and the other two can partly
  come from the 4-bit quantization. The paper states it.
- Not comparable with the 2026-10-18/19 Qwen runs (OpenRouter, Phala). Comparable in weights with
  the v1 local runs (the same Ollama build), not in anything else (other guard, cases, classifier).
- Cost: $0 in model API (only the embedding is billed). Time: estimated 15–30 hours, measured in
  the run's `kit-log.jsonl`.
- Reproducible without a provider: the weights are pinned by the Ollama tag and its digest, which
  the run records.
