# ADR-0019: The demo console picks the live model per run

- Status: accepted (owner, Phase 9: "cho phép chọn Model bằng nút Live")
- Date: 2026-11-18
- Amends: ADR-0018 decision 4 ("M1": one live provider, from `LLM__*`); R25.4
- Task: 9.16

## Context

ADR-0018 gave a demo job two model profiles: `fake` and `live`, where `live` is the one provider
the stack's `LLM__*` settings configure. The defence compares the benchmark's three v3 models
(gpt-4o-mini, Qwen2.5-7B and Llama-3.1-8B on OpenRouter). With one live provider, showing a
second model means editing `.env` and recreating three services in front of the committee.

## Decision

1. A catalog, `config/live_models.yaml` (`DEMO__LIVE_MODELS_PATH`), lists named live models:
   endpoint, model id, OpenRouter provider pin, prices, and the name of the environment variable
   that holds the key (`DEMO_OPENAI_API_KEY`, `DEMO_OPENROUTER_API_KEY`). Keys stay in the
   environment; the catalog holds variable names only. The entries carry the endpoints, pins and
   prices of `evaluation/mailguard_bench/model_profiles.py`; a unit test keeps the two equal, and
   `packages/` still never imports `evaluation/`.
2. `run_options` gains `live_model` (a catalog name, or null). It travels with the job like the
   other run options (JSONB column and envelope field of task 9.2; no migration) and is kept on
   retry and replay.
3. The triage-worker and the ai-worker build one provider per catalog model whose key is set, next
   to the `LLM__*` one. `ProfileRoutedLLMProvider` picks per call: `fake`, or the named catalog
   model, or the `LLM__*` provider, or the only keyed catalog model when the job names none.
   The guard's live judges follow the same choice (owner decision C14 unchanged: judges use the
   job's live model).
4. The API refuses, before publishing anything, a live run that names an unknown or keyless model,
   or names none when no single live model is implied (422 `LIVE_MODEL_UNAVAILABLE`, R25.4).
   `GET /v1/demo/capabilities` lists the catalog with `available` per model; the console shows a
   keyless model greyed out.
5. Catalog prices join the cost counters' price table; an `LLM__PRICE_TABLE` entry for the same
   model wins (R21.6).

## Consequences

- One generation call per job is unchanged: the picker chooses which provider serves it.
- The embedding model never switches (R5.10).
- A stack with no catalog key and `LLM__PROVIDER=fake` behaves exactly as before: Live is off.
- A wrong key shows up as a failed live job (retry ladder, then `DEAD_LETTER`), not at start-up.
