# ADR-0018: the triage LLM's category is an enum on the wire, with a larger output budget

- **Status:** Accepted, 2026-11-09
- **Date:** 2026-11-09
- **Decided by:** project owner (chat review of 2026-11-09: "làm theo đề xuất")
- **Number:** 0018 (ADR-0016 is reserved for task 7.32)
- **Task:** `specs/tasks.md` 7.35
- **Amends:** nothing; it tightens how stage 3 of triage (R6.1, R6.3) meets R6.4
- **Changes:** `services/triage_worker/llm_classifier.py`, `tests/unit/test_triage_stage3.py`,
  `specs/tasks.md`. **Not** `agentmailguard/`, so the guard pin (`d95e542`) and its tree hash stay.

## Context

The 1,800-case runs of 2026-10-19 lost cases to triage, not to the guard (raw/services.C3.log of
each run, the triage-worker's own warnings):

| Model | Error | Count in C3 | Kind |
|---|---|---|---|
| Qwen2.5-7B | `Category 'automation_notification'` (also `automation`, `automation_instructions`) `not recognized` | 128 | the model invented a category |
| Llama-3.1-8B | `temporarily rate-limited upstream` | 43-44 | provider (OpenRouter, CoreWeave) |
| Llama-3.1-8B | `Unterminated string` in the JSON, cut inside `reasoning` (char 197-209) | 6-10 | answer cut |
| gpt-4o-mini | none | 0 | |

All three models get the same request: `response_format` `json_schema` with `strict: true`,
temperature 0, `max_tokens` 250. The schema declared `category` as a free string, described as
"one of the 9 canonical email categories"; the nine were checked only after the answer, by the
pydantic validator. Strict mode constrains the shape, not a free string's value, so a model could
emit any string. A rejected answer is a stage error, the email falls to the R6.11 safe default,
and the benchmark records the case as a `triage_stage_failure` error row (Amendment 3) and leaves
it out of the score. That is why Qwen scores about 984 of 1,000 email attacks and gpt-4o-mini 1,000.

The cut Llama answers stopped inside `reasoning`, a free-length field. The longest successful
triage answer of the three runs was 219 tokens (Llama), close to the 250-token budget. The logs did
not record the stop reason, so a length stop could not be told from a provider cut.

Both pinned OpenRouter endpoints list `structured_outputs` and `response_format` among their
supported parameters (OpenRouter `/api/v1/models/<slug>/endpoints`, read 2026-11-09: Phala for
`qwen/qwen-2.5-7b-instruct`, CoreWeave bf16 for `meta-llama/llama-3.1-8b-instruct`).

## Decision

1. **`category` is an `enum` of the nine canonical categories on the wire** (`_strict_wire_schema`,
   from `CANONICAL_CATEGORIES`, so the list has one source). Strict decoding can then emit only a
   listed value. The validator stays, so a provider that ignores the schema is still caught and an
   alias is still normalised.
2. **The stage-3 output budget is 400 tokens** (`TRIAGE_MAX_TOKENS`, was 250).
3. **`reasoning` is asked to stay under 160 characters in its description**, not with `maxLength`:
   OpenAI's Structured Outputs guide lists only `pattern` and `format` as supported string keywords,
   and a strict schema with an unsupported keyword is refused.
4. **A failed stage-3 call logs the provider's stop reason** (`finish_reason=...`), so a length stop
   is visible next time.
5. **The provider pin is not changed.** Llama's rate-limit errors are a provider condition; ADR-0014
   (one pinned provider, fallbacks off) stands. The kit's retry pass already re-runs those rows; a
   run lowers its concurrency if they persist.

## Consequences

- Triage behaviour changes for every config, C0 included, since C0 runs the same triage. New runs
  are not comparable case by case with the 2026-10-19 runs; that comparison is already broken by
  the new case set and classifier of task 7.34/7.36, and the reports say so.
- Fewer emails should fall to the safe default for the small models. Some emails Qwen used to lose
  will now be classified, so they are scored instead of excluded; this can move Qwen's attack
  success rate either way, and the report states the scored count as before.
- A larger budget can cost slightly more per triage call; measured answers were 72-219 tokens, so
  the budget is a ceiling, not the usual length.
- No requirement ID changes.
