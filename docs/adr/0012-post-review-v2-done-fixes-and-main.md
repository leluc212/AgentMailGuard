# ADR-0012: After the thesis review — v2's definition of done, the Llama-run fixes, guard fixes on the guard's branch, and one repository on main

- **Status:** Accepted
- **Date:** 2026-09-30
- **Decided by:** project owner (decision round, 2026-09-30 22:25)
- **Relates to:** ADR-0010 (its clause "the branches are not merged, and nothing goes to `main`" is
  superseded when decision 6 is carried out), ADR-0011, `docs/superpowers/specs/2026-09-29-mailguard-live-v2-design.md`,
  `STATUS-desktop.md` footnote 4, tasks 7.20, 7.21, 7.22

## Context

The v1 benchmark and the 2026-09-30 thesis review left four open threads: v2 ("every service live")
was built in six work packages but never integrated or run; the local Llama run exposed defects in the
guarded path and in reporting; a teammate must be able to run the benchmark from a fresh download; and
rag-email and AgentMailGuard live on separate branches with unrelated histories, while `main` holds only
a first commit.

## Decisions

1. **v2 is done** when every service, feature and function of rag-email is configured on and shown
   working live — in the owner's words, "everything is configured to turn on and actually has all
   functions, features and services working" — shown on this desktop by live smoke runs through every
   config and every model route. **The full v2 benchmark is run by the owner's teammate** with the
   benchmark kit (decision 9), so the kit itself must be proven end to end from a fresh download. Numbers
   are reported as they come out; v2 being done does not depend on the guard meeting a target. The
   checklist lives in the v2 design.
2. **Fixes before any v2 run.** Bug fixes only, each with a failing test first, and no detection tuning:
   (a) the guarded prompt carries rag-email's reply-format instruction (the cause of Llama's
   greeting-only drafts); (b) a failed AI step in L2 is never silent; (c) no matching policy rule can weaken a stricter action: the strictest matching action wins (today P00,
   human approval, outranks P01, quarantine, and P04/P05, human approval, outrank P06, block); (d) reports price a run
   from the run's own model profile; (e) benign utility counts greeting-only drafts as failures,
   pre-registered for v2 before its runs. Not changed: detection thresholds, rules, detection prompts,
   pinned cases. v1 results stay as published.
3. **Guard fixes (b) and (c) are committed directly on `feature/mailguard-defense-stack`** with the owner's approval (commits a5c2cc4, c626a84, 006662e, 1a3ef62), and `1a3ef62` is pinned for v2.
   v1 stays pinned to `81df5d07`. A consequence of (b): L2's structured answer now requires all six fields,
   so a model that omits one gets one repair attempt and then a visible fallback instead of silent defaults.
4. **When L2's AI step fails** (non-JSON answer, null or invalid fields, timeout), L2 keeps its rule and
   classifier result, records the failure, the email is counted normally, and reports give the
   fallback rate per config.
5. **Task 7.21:** option (a) — the router's relevance check applies only when the rerank really ran.
6. **One repository on `main`:** rag-email at the root and AgentMailGuard under `agentmailguard/` with
   its full history (subtree merge); rag-email's Makefile then uses `./agentmailguard` by default.
   `RAG_Email_System`, `desktop-live` and `feature/mailguard-defense-stack` are kept, plus backup tags.
   This is carried out only after decision 1 holds and a final audit finds no open issue, and only on
   the owner's explicit go.
7. **The meaning-column reader model is the runner's choice** and must not be one of the benchmarked
   models; the tooling refuses such a choice.
8. **How the work is done:** Sonnet 5.5 builders in workflows, test-first with an adversarial review
   per change; pushes to `desktop-live` as work lands; `main` only at the end.
9. **The teammate benchmark kit** (owner round, 2026-09-30 22:38): the teammate uses a Windows 11 laptop
   (Intel Core i7-13700HX, 24 GB RAM, NVIDIA RTX 4050 Laptop GPU with 6 GB, about 63 GB of free disk), has
   admin rights, and runs all
   three models as we did: gpt-4o-mini by API, Qwen2.5-7B and Llama-3.1-8B locally. The kit is WSL2-first
   (WSL2 Ubuntu plus Docker Desktop with WSL integration, everything run inside Ubuntu), with a separate
   guide for running natively on Windows. The teammate uses their own API keys; results come back the way
   ours do (run folders and the status page committed to a branch). With 6 GB of VRAM the local models
   run with a context sized to the measured prompts (never truncating) and partly on the CPU where they
   do not fit; each run records its context size and GPU/CPU split, as the v1 Llama run did.
   **Proposed the same evening (owner, 23:01), then deferred (owner, 23:17) to the team meeting of
   2026-10-01, around 20:00:** running Qwen2.5-7B and Llama-3.1-8B through OpenRouter's API instead of
   locally, with gpt-4o-mini staying on OpenAI's, so that no local GPU is used. If it is chosen, each
   OpenRouter run pins one provider with fallbacks off and strict JSON-schema support required, and
   records the provider that served every call; the pinned provider and its precision are chosen from
   OpenRouter's current listing before the runs, and reports say that v2's Qwen and Llama numbers are
   then not directly comparable with the v1 local 4-bit runs. Until that decision the local route above
   stands; the OpenRouter support is prepared on branches that are not merged.
10. **Scope of "live" for v2** (owner, same round): every part of rag-email and AgentMailGuard runs live,
   except the mail providers' fetch and send: the benchmark's cases (Microsoft's LLMail-Inject emails and
   the poisoned-document cases) enter right after the mailbox-fetch step, as ADR-0011 designs, and nothing
   is sent. A live Gmail or Outlook round trip is not part of v2.

## Consequences

- v2 results will be produced by a different guard commit than v1; every report records both commits,
  and v1 and v2 numbers are never mixed.
- The v2 design gains an explicit definition of done, and task 7.20's status is rewritten against it.
- Until decision 6 is carried out, AgentMailGuard stays a git worktree of its own branch.
