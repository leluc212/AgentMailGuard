# Desktop benchmark status

Updated 2026-09-29 19:55 +07 by the desktop Claude Code session. Numbers come only from each run's
committed `report.md` / `summary.json`; "pending" means not finished yet. v1 = rag-email's reply path
in-process (same pinned cases `sha256=c00dddca…`, AgentMailGuard `81df5d07`, runs on commit `2a61925`).

| RUN | Model (serving) | State | C3 ASR, LLMail [Wilson 95 %] | Target | C0 ASR | C0T ASR | C3 FPR | Benign utility | Errors C0 / C0T / C3 | Cost | Wall time |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `2026-09-29-gpt4omini` | gpt-4o-mini (OpenAI API) | done, independently re-verified | 0.0 % [0.0, 1.3] (0/300) | met | 54.2 % [48.5, 59.7] (162/299) | 43.5 % [38.0, 49.1] (130/299) | 0.0 % [0.0, 2.5] (0/150) | 100 % (150/150) | 1 / 1 / 0 | ≈ $0.42 ¹ | 43 min |
| `2026-09-29-qwen25` | qwen2.5:7b-instruct (Ollama, 4-bit, 32k ctx) | running: C0, C3, C0T done with 0 errors; C1, C2 left | pending | pending | pending | pending | pending | pending | 0 / 0 / 0 so far | $0 API | pending |
| `2026-09-29-llama31-local` | llama3.1:8b (Ollama, 4-bit, 32k ctx) | starts after Qwen (≈ 20:45) | pending | pending | pending | pending | pending | pending | – | $0 API | pending |

¹ Recorded tokens × the profile's price ($0.15 / $0.60 per 1M); `report.md` prints "unknown: unpriced"
because the report step does not know the profile's prices.

gpt-4o-mini RAG vector (poisoned knowledge documents): C0 79.0 % [70.0, 85.8], C0T 66.0 % [56.3, 74.5],
C3 32.0 % [23.7, 41.7]; the poisoned document was retrieved in 100/100 cases.

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
