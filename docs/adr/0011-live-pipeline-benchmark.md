# ADR-0011: The v2 benchmark runs every rag-email service live; AgentMailGuard runs as an evaluation guard-worker outside rag-email

- **Status:** Accepted
- **Date:** 2026-09-29
- **Decided by:** project owner (grilling session, 2026-09-29)
- **Requirements:** R22.12, R20.6, R24.5, R4.1, R5.10, R6.1, R6.3, R8.3, R10.9, R11.1–R11.5
- **Relates to:** ADR-0010, `CLAUDE.md` §4 and §6, `docs/superpowers/specs/2026-09-29-mailguard-live-v2-design.md`
- **Changes:** `specs/tasks.md` task 7.20, `docs/demo-runbook.md` §9.9, `docs/configuration.md`, `docker-compose.yml`

## Context

The v1 benchmark (task 7.19, ADR-0010) puts each case into rag-email's reply path in-process, on a mock embedder, and its reply path has no reranker. Its own report lists what else that leaves out: the mailbox fetch, MinIO, the MIME parser and cleaner, the queues and workers, and triage. Attacks that ride on those stages are not measured, and neither is the effect of triage stopping an attack before the guard sees it.

The owner wants a benchmark that is production-faithful, and the guard cannot simply move into the product: `CLAUDE.md` §6 and requirements §0.5 exclude prompt-injection defences from rag-email ("designed as separate cross-cutting subsystems"), and ADR-0010 decided that rag-email calls AgentMailGuard only through AgentMailGuard's own adapters, adds no defence logic, and leaves wiring the guard into the live ai-worker as "a later, separate decision".

## Decision

The owner decided, on 2026-09-29:

1. **"Live" means production-faithful.** Every AI step and every service of rag-email runs for real. One benchmarked model serves every LLM role of a run: triage stage 3, the summarizer, generation and the guard's judges. Gemini `gemini-embedding-001` at 1536 dimensions is shared by all runs, so the model under test is the only variable.
2. **Triage runs live, with two ASRs.** The pipeline ASR counts a triage-stopped attack as no success; the guard ASR counts only attacks that reached drafting. The C3 target is judged on the guard ASR, and both are always reported with Wilson intervals. All guard LLM stages (L1, L2, L3b, L4) are on in C3.
3. **AgentMailGuard is an evaluation guard-worker** that takes over the ai-worker's drafting step for the guarded configs (C0T, C1, C2, C3); C0 keeps the ai-worker container. See below.
4. **Emails enter where the mail-connector hands off.** See below.
5. **Fixes and wiring that live runs need:** triage's stage-3 JSON schema meets OpenAI Structured Outputs; the summarizer honours `SUMMARIZATION__SUMMARIZER_MODEL`; the retrieval budget default goes from 500 to 3000 ms; the missing `html` bucket is bootstrapped and bucket names come from settings; the embedder tolerates response items without `index` and a response without `usage`; the cross-encoder reranker is wired into the reply path (R11.1–R11.5).
6. **Scoring:** the official string-match score stays, and a pre-registered meaning-based second column is added (rubric v1, fixed before any v2 run, read by a model that is not one of the benchmarked models and is recorded before the runs).
7. **Nothing is ever approved or sent.** Drafts stay in the review queue, so the dispatch-worker never receives a job.
8. **v1 stays as it is.** v2 runs use new `RUN` names and a `transport: services-v2` fingerprint key, so the two never mix.

### Where the guard runs

```
 rag-email: no defence logic            evaluation/ (host process)              AgentMailGuard (pinned worktree)
 ┌──────────────────────────────┐      ┌───────────────────────────────┐      ┌──────────────────────────┐
 │ services/ai_worker/main.py   │      │ live/guard_worker.py          │      │ MailGuardPipeline, L1-L5 │
 │ build_consumers(...,         │◀─────│  GuardedDraftingService       │─────▶│  through its own         │
 │   drafting_factory=None)     │ hook │  (the ai-worker's own code)   │      │  integration adapters    │
 └──────────────────────────────┘      └───────────────────────────────┘      └──────────────────────────┘

 email.<category>.<priority>  (lane queues)
        │
        ├── C0 ────────────────────▶ ai-worker container   rag-email's own DraftingService
        └── C0T, C1, C2, C3 ───────▶ guard-worker (host)    GuardedDraftingService, same ai-worker code
 Exactly one of the two consumes at a time; the live runner refuses to start otherwise.
```

The guard-worker is a host process in `evaluation/`. It runs `services.ai_worker.main.build_consumers` unchanged, apart from one new keyword-only argument, `drafting_factory`: a generic injection point that receives the arguments `DraftingService` takes today and returns the drafting service to use. rag-email learns no guard names from it. The guard's orchestration stays in `evaluation/mailguard_bench/guarded_reply.py` and is reused, not copied.

**Why not inside rag-email.**

- `CLAUDE.md` §6 and ADR-0010: a guard imported into the ai-worker is a defence subsystem inside rag-email, which is what both decided against. The hook adds an extension point, not a defence.
- The runtime image installs exactly what `uv.lock` pins, without dev dependencies, while AgentMailGuard is a git worktree pinned by commit and installed editable per command (`uv run --with-editable`). Baking it into the image would change that dependency model.
- AgentMailGuard ships top-level `services` and `evaluation` packages of its own. In one Python environment they shadow rag-email's, which is why every benchmark command runs as `python -m` from the rag-email root (`guard_env.require_module_origins`). A container holding both would need the same care on every start.

### Where emails enter

The feeder replaces only the mailbox fetch. For each case it builds a `text/plain` UTF-8 MIME message and does what `services/mail_connector` does after a fetch: archive the raw message (`RawPayloadArchiver`, `packages/core/archive.py`) into MinIO's `raw-mime` bucket, create the `processing_job` and publish it to `email.normalize`, reusing the connector's archiver and orchestrator code rather than a copy. From `email.normalize` on, the email-worker, triage-worker, lane queues and drafting consumer are the production path. The case's knowledge documents go through the API's upload endpoint (`POST /v1/knowledge/documents`), MinIO's `knowledge-docs` bucket, `knowledge.ingest` and the knowledge-worker, which chunks and embeds them with Gemini.

The feeder needs a mailbox row to attach the email to. `create_eval_mailbox` in `packages/adapters/evaluation.py` inserts one with provider `imap` and `credentials_ref` `eval/none`, and the feeder is its only caller. The provider name stays inside `packages/adapters/` (`CLAUDE.md` §4), and `credentials_ref` is a reference, never a secret (R1.1). Every tenant query carries the throwaway organization's id (`CLAUDE.md` §4), and cleanup deletes the organization's MinIO objects in `raw-mime`, `attachments`, `html` and `knowledge-docs`, then the organization row (cascade).

## Consequences

- **The benchmark measures the whole pipeline**, and separates what triage stops from what the guard stops. Cost, latency and tokens now include real embeddings, the reranker and the queues.
- **Not measured, still.** The provider's own fetch and sync, and the MIME a real provider delivers: the feeder's messages are plain UTF-8 text, so attacks carried by HTML or attachments remain outside the results, as the v1 report already says.
- **Runs are sequential.** v1 ran two models at once in separate host processes. In v2 the containers hold one model's settings and the lane queues one drafting consumer, so one model runs at a time and one config at a time (C0 on the container, then each guarded config on the guard-worker).
- **A placement difference for latency.** C0 drafts in a container and the guarded configs in a host process that reaches Postgres, RabbitMQ and MinIO through published ports. The size of that difference is not measured. The ASR does not depend on it, but read a latency comparison between C0 and the guarded configs with it in mind.
- **Ops changes.** Compose gives the four services that call a model a `host.docker.internal` alias; a per-model `.env.stack` (`evaluation/mailguard_bench/live/stack_env.py`) carries the model and embedding settings and is applied with `docker compose up -d --no-deps` to those services only; Postgres, RabbitMQ and MinIO are never restarted. A local Ollama must listen on the Docker bridge address, which the owner sets up with `sudo` (`docs/demo-runbook.md` §9.9).
- **A file with keys.** `.env.stack` holds API keys. It is git-ignored, kept out of every docker build, written owner-only, and refused for any path git could commit; the tool never prints a key, and the owner deletes the file after the runs. This is hygiene for an owner-run tool, not a security control (`CLAUDE.md` §6).
- **Shared quotas.** Every run spends the same Gemini key on `gemini-embedding-001` (case knowledge at ingestion, one query per email that needs retrieval), whichever model is under test.
- **Truncated embeddings.** Google's documentation says that with `gemini-embedding-001` the caller must normalise vectors of any size other than 3072. Retrieval ranks by cosine distance (`vector_cosine_ops`), which ignores length, so no normalisation step is added.
- **Two seams exist for evaluation only:** `drafting_factory` and `create_eval_mailbox`. Neither contains defence logic or a guard name. If the guard is ever wired into the live ai-worker, ADR-0010 item 3 applies: that is a separate decision.

## Alternatives rejected

- **The guard inside the ai-worker image or as a compose service.** Against `CLAUDE.md` §6 and ADR-0010, and it needs the pinned worktree and the package-name care above inside a container.
- **A copy of the ai-worker's consumer in `evaluation/`.** It would drift from the production code the benchmark is meant to measure; the hook lets the guard-worker run the real one.
- **Keep the v1 in-process harness as the only benchmark.** The owner rejected it as not production-faithful.
- **Judge the C3 target on the pipeline ASR.** Triage-stopped attacks would count as guard successes and flatter the guard; the guard ASR is the target, and the pipeline ASR is shown beside it.
- **A different embedding model per benchmarked model.** Retrieval would differ between runs, and the model under test would no longer be the only variable.
- **Ollama on every interface (`0.0.0.0`) as the default.** Ollama has no login of its own, so that opens its API to the whole network. The bridge address is enough for the containers.
