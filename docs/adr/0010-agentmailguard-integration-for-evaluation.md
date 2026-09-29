# ADR-0010: AgentMailGuard is integrated as the separate prompt-injection subsystem; rag-email adds no defence logic

- **Status:** Accepted
- **Date:** 2026-09-28
- **Decided by:** project owner
- **Relates to:** `CLAUDE.md` §6, `specs/requirements.md` §0.5, `docs/superpowers/specs/2026-09-29-mailguard-benchmark-design.md`

## Context

`CLAUDE.md` §6 and `requirements.md` §0.5 exclude prompt-injection defences from rag-email and say they are "designed as separate cross-cutting subsystems". AgentMailGuard, on branch `feature/mailguard-defense-stack` (separate history, no shared commits), is that subsystem: a five-layer guard (L1 inbound scanner, L2 intent extraction, L3/L3b channel isolation and retrieved-chunk scanning, L4 output scanner, L5 policy) with integration adapters that work with rag-email's `ContextPackage` and `LLMProvider` without importing them. AgentMailGuard is the project's main contribution. rag-email was built as its host because no company's RAG email system was available to integrate with, so AgentMailGuard must be evaluated running inside rag-email, with and without the guard, on the same attacks.

## Decision

1. rag-email calls AgentMailGuard **only through AgentMailGuard's own integration adapters**. rag-email's code gains no detection rules, classifiers, sanitisers or policies of its own; §6 stays true for rag-email's code.
2. AgentMailGuard is consumed as a package: a git worktree of its branch, installed editable into rag-email's environment. The branches are not merged, and nothing goes to `main`.
3. The first use is evaluation (the C0 vs C3 benchmark). Wiring the guard into the live ai-worker is a later, separate decision.

## Consequences

- The benchmark measures rag-email's real pipeline, and the guard's effect on it, without moving security design into rag-email.
- AgentMailGuard's version is pinned by the worktree's commit, recorded in each benchmark report.
- Any defence change found necessary is made in AgentMailGuard's branch, not in rag-email.
