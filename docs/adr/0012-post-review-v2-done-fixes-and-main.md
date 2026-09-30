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
   functions, features and services working" — and the v2 benchmark runs through all of them for the
   three models, with every reported number independently re-verified. Numbers are reported as they
   come out; v2 being done does not depend on the guard meeting a target. The checklist lives in the v2
   design.
2. **Fixes before any v2 run.** Bug fixes only, each with a failing test first, and no detection tuning:
   (a) the guarded prompt carries rag-email's reply-format instruction (the cause of Llama's
   greeting-only drafts); (b) a failed AI step in L2 is never silent; (c) a layer error never weakens a
   stronger policy action (today P00, human approval, outranks P01, quarantine); (d) reports price a run
   from the run's own model profile; (e) benign utility counts greeting-only drafts as failures,
   pre-registered for v2 before its runs. Not changed: detection thresholds, rules, detection prompts,
   pinned cases. v1 results stay as published.
3. **Guard fixes (b) and (c) are committed directly on `feature/mailguard-defense-stack`** with the
   owner's approval, and the resulting commit is pinned for v2. v1 stays pinned to `81df5d07`.
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
9. **The teammate benchmark kit** must work for a teammate on Windows; its approach is decided in a
   follow-up round and recorded as an amendment to this ADR.

## Consequences

- v2 results will be produced by a different guard commit than v1; every report records both commits,
  and v1 and v2 numbers are never mixed.
- The v2 design gains an explicit definition of done, and task 7.20's status is rewritten against it.
- Until decision 6 is carried out, AgentMailGuard stays a git worktree of its own branch.
