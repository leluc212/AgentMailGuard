# Desktop benchmark status

Updated 2026-09-29 20:40 +07 by the desktop Claude Code session. Numbers come only from each run's
committed `report.md` / `summary.json`; "pending" means not finished yet. v1 = rag-email's reply path
in-process (same pinned cases `sha256=c00dddca…`, AgentMailGuard `81df5d07`, runs on commit `2a61925`).

| RUN | Model (serving) | State | C3 ASR, LLMail [Wilson 95 %] | Target | C0 ASR | C0T ASR | C3 FPR | Benign utility | Errors C0 / C0T / C3 | Cost | Wall time |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `2026-09-29-gpt4omini` | gpt-4o-mini (OpenAI API) | done, independently re-verified | 0.0 % [0.0, 1.3] (0/300) | met | 54.2 % [48.5, 59.7] (162/299) | 43.5 % [38.0, 49.1] (130/299) | 0.0 % [0.0, 2.5] (0/150) | 100 % (150/150) | 1 / 1 / 0 | ≈ $0.42 ¹ | 43 min |
| `2026-09-29-qwen25` | qwen2.5:7b-instruct (Ollama, 4-bit, 32k ctx, 100 % GPU) | done 16:36–20:29 | 0.0 % [0.0, 1.3] (0/300) | met | 51.7 % [46.0, 57.3] (155/300) | 48.3 % [42.7, 54.0] (145/300) | 0.0 % [0.0, 2.5] (0/150) | 100 % (150/150) | 0 / 0 / 0 | $0 API (local) | 3 h 53 min |
| `2026-09-29-llama31-local` | llama3.1:8b (Ollama, 4-bit, **16k ctx** ², 100 % GPU) | running since 20:31 on commit `dbe8e6b` | pending | pending | pending | pending | pending | pending | 0 so far | $0 API (local) | expected ≈ 00:30–01:00 |

¹ Recorded tokens × the profile's price ($0.15 / $0.60 per 1M); `report.md` prints "unknown: unpriced"
because the report step does not know the profile's prices.

² At 32k context Llama did not fit the 12 GB GPU (6 % on the CPU, 9 tok/s, a 20-hour run), so its
local model got `num_ctx 16384` (same weights, local digest `2b898909a7df`, original `46e0c10c039e`). The
largest prompt any v1 call sent is about 5.8k tokens, so nothing is truncated; see the run's
`ollama-state.txt`. Qwen's run was already finished at 32k.

RAG vector (poisoned knowledge documents), C0 / C0T / C3 ASR:
gpt-4o-mini 79.0 % [70.0, 85.8] / 66.0 % [56.3, 74.5] / 32.0 % [23.7, 41.7];
qwen2.5-7b 91.0 % [83.8, 95.2] / 72.0 % [62.5, 79.9] / 39.0 % [30.0, 48.8].

C3 latency p50 / p95 per email: gpt-4o-mini 2.78 s / 5.45 s; qwen2.5-7b 4.98 s / 13.87 s (local GPU, one
worker; SC5's 10 s p95 is not met).

## Caveats a presenter must state (all verified)

- **Reply path only.** Cases enter rag-email as already-cleaned text; the mailbox fetch, MinIO, the MIME
  parser and cleaner, the queues and workers, and triage are not exercised, so attacks carried by MIME
  structure, HTML or attachments are not measured.
- **Mock embedder.** Every v1 run (the laptop's Gemma run too) used the mock embedder, so RAG retrieval
  was effectively keyword-based.
- **Guard stages.** C3 ran with L3b's and L4's LLM stages off (review-1 plan decision); only the L1 judge
  and L2 used the model.
- **Scoring rule.** The string-match rule inflates the C0/C0T baselines: reading gpt-4o-mini's 299 C0
  drafts, about 88 (29 %) really carried out the attack, against 162 (54 %) by the rule. C3's 0/300 holds
  either way. A pre-registered meaning-based column comes with v2.
- **Who blocked.** For gpt-4o-mini, 281 of the 287 C3 blocks were decided by L1's local classifier (211)
  and rules (70); the model's judge decided 6.
- **FPR data.** 138 of 150 benign emails are near-duplicates of L1 training negatives; on the other 12,
  FPR is 0/12 [0.0, 24.3].
- **RAG vector.** The ≤ 5 % target is stated for LLMail; the RAG vector is far from it.
- **Llama** is the local 4-bit build, not OpenRouter's (the OpenRouter account had no credit).

## Next

v2 "every service live" (design: `docs/superpowers/specs/2026-09-29-mailguard-live-v2-design.md`) is
being built on the desktop and will not be ready for the 2026-09-30 morning presentation.
