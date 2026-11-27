# ADR-0020: The demo's live model catalog is no longer a copy of the benchmark's profiles

- Status: accepted (owner, 2026-11-27: "dùng qwen/qwen3.5-9b", "bỏ bớt những cái thừa", option 3)
- Date: 2026-11-27
- Amends: ADR-0019 decision 1 (the catalog carries the benchmark's three v3 models and a test
  keeps it equal to `model_profiles.py`); task 9.16
- Task: 9.18

## Context

The first keyed live run of the console (owner's OpenRouter key, 2026-11-27) showed:

- `qwen2.5-7b-openrouter` fails every call with HTTP 404: OpenRouter withdrew
  `qwen/qwen-2.5-7b-instruct` ("no provider serves this model anymore"). The model cannot be
  served on this route at any pin.
- `gpt-4o-mini` needs a direct OpenAI key (`DEMO_OPENAI_API_KEY`) the owner does not have. The
  same model already runs as the `LLM__*` provider through OpenRouter (`openai/gpt-4o-mini`), so
  the entry only ever showed greyed out.
- `llama-3.1-8b-openrouter` still works at its CoreWeave `bf16` pin.

## Decision

1. `config/live_models.yaml` lists the models the console can actually serve:
   `llama-3.1-8b-openrouter` (unchanged, still equal to its benchmark profile) and
   `qwen3.5-9b-openrouter` (`qwen/qwen3.5-9b` on OpenRouter, $0.10 / $0.25 per 1M tokens,
   no provider pin). `gpt-4o-mini` and `qwen2.5-7b-openrouter` leave the catalog.
2. The unit test no longer requires the catalog to equal the benchmark's three models. It
   requires that every catalog entry that shares a name with a benchmark profile carries that
   profile's endpoint, model id, prices and pin, so a benchmark model shown in the console is
   still the model the benchmark ran.
3. `evaluation/mailguard_bench/model_profiles.py`, the benchmark's results and its runbook are
   unchanged. The committed v3 results of Qwen2.5-7B stay valid as history; they can no longer
   be reproduced on OpenRouter.

## Consequences

- A console run on Qwen3.5-9B is not comparable with the benchmark's Qwen2.5-7B rows; the label
  does not claim it is.
- Qwen3.5-9B is unpinned, so the serving provider may change between calls. Its runs are demo
  runs, not measurements.
- `DEMO_OPENAI_API_KEY` stays read by the code (ADR-0019 decision 3) but no catalog entry names
  it until one is added back.
