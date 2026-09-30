# AgentMailGuard layer ablation — pre-registered design (task 7.22)

- **Status:** pre-registered 2026-09-30, before any ablation code or run.
- **Decided by:** project owner (2026-09-30 evening, after the thesis review).
- **Relates to:** `2026-09-29-mailguard-benchmark-design.md` (v1 benchmark), ADR-0010, requirement R22.12.

## Why

At the 2026-09-30 review, reviewers asked what each AgentMailGuard layer takes in, how it decides and
what it outputs, and why layers 2 to 5 are needed when layer 1 stops most attacks in the v1 results.
The v1 benchmark cannot answer the second question: its C1/C2/C3 presets add layers on top of L1, so
L1 always acts first and gets the credit. This experiment removes one layer at a time from the full
guard, including L1, on the same cases, so each layer's own contribution is measured.

## What runs

| Config | Guard preset (AgentMailGuard `81df5d07`) | Meaning |
|---|---|---|
| C0 | none (rag-email's own path) | same-run no-guard baseline |
| C3 | `C3` | same-run full-guard baseline |
| C3-L1 | `C3-L1` | every layer except L1 (the "L1 fails" case) |
| C3-L2 | `C3-L2` | every layer except L2 |
| C3-L3 | `C3-L3` | every layer except L3 (channel isolation) |
| C3-L3B | `C3-L3B` | every layer except L3b (retrieved-document scanner) |
| C3-L4 | `C3-L4` | every layer except L4 (output scanner) |
| C3-L5 | `C3-L5` | every layer except L5 (policy engine) |

- **Cases:** the pinned v1 case set (`cases_sha256 c00dddca…`), all 550 per config: 300 LLMail-Inject
  attacks, 100 RAG-vector attacks, 150 LLMail benign emails. No new or changed cases.
- **Model:** gpt-4o-mini (OpenAI API) writes the reply and serves the guard's LLM stages, as in v1;
  concurrency 2. Qwen2.5-7B may replicate later under the same design. Llama-3.1-8B is excluded until
  the guard template's missing reply-format instruction is fixed (STATUS-desktop.md footnote 4).
- **Code:** the v1 transport at `dbe8e6b` plus only the support needed to run these presets. No rule,
  threshold, prompt, template or case change. L3b's and L4's LLM stages stay off, as in v1.

## What is measured

- ASR per vector (LLMail email, RAG) by the official string-match rule, with Wilson 95 % intervals
  (z = 1.96), and C3 FPR on the 150 benign emails.
- For each ablation config against the same-run C3: an exact paired McNemar test on the same case ids.
- Secondary: benign drafts with real content (not blocked, 40 characters or more), and, per attack,
  which layer blocked it and which layers flagged it.

## Hypotheses (stated before running)

- **H1 (L1 off):** C3-L1's LLMail ASR stays far below the same-run C0's, i.e. layers 2 to 5 stop most
  LLMail attacks without L1.
- **H2:** removing L4 raises the RAG-vector ASR over C3 (in v1, L4's output checks made every RAG block).
- **H3:** removing L3b does not lower the RAG-vector ASR.
- **H4:** on LLMail, removing any single one of L2, L3, L3b, L4 or L5 changes the ASR by less than 5
  points, because L1 stops almost all of those attacks first.

## Decision rule

A layer is **measurably necessary on this benchmark** if removing it raises the ASR on some vector with
McNemar p < 0.05 against the same-run C3. Otherwise it is **not measurable on this benchmark**: that is
reported as it is, next to the attack classes the layer covers that these cases do not contain.
Every config is reported, including null results. Error rows get the documented retry pass only.

## Known limits

The attacks come from LLMail-Inject and PoisonedRAG plus seed documents. Classes where L1 is weakest
(paraphrases without trigger words, injections in quoted history, persona hijacks) are scarce here, so
H4 may hold even for layers built for those classes. A class-targeted attack set is future work.
