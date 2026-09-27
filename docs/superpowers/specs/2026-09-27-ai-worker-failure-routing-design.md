# AI-Worker Consumer Core and Generation Failure Routing (task 4.13a, closes 4.9) — Design

**Status:** approved in conversation on 2026-09-27. The user chose option 1 ("a draft that fails validation twice goes straight to the DLQ"), approved this design, and approved splitting 4.13 into 4.13a (now) and 4.13b (later).

## Problem

Task 4.9 (structured output and validation) stays `[~]` because nothing consumes generation jobs, so no one can show where a generation failure ends up. `BaseConsumer.is_transient_error` retries everything that is not a `FatalError`. Four failure kinds therefore climb the whole retry ladder (30 s, 5 min, 30 min) before reaching the dead-letter queue, with up to 8 billed model calls per job:

- a draft invalid after repair (`UnvalidatedDraftError`);
- a misconfigured profile schema (`DraftSchemaContractError`);
- a message without a thread (`UnpersistableDraftError`);
- a redelivery of an already-drafted job (`IllegalStateTransitionError`).

## Decision and sources

Retry only transient faults. Send non-transient failures to the error queue at once with the reason attached, and let a human replay them. This follows:

- Microsoft's Retry pattern: cancel when "the failure isn't transient", and place retry policy "only where the full context of a failing operation is understood".
- NServiceBus recoverability: "unrecoverable" exceptions skip retries.
- OpenAI Structured Outputs: non-matching output comes from refusals or `max_tokens` truncation.
- RabbitMQ: every queue should have a dead-letter configuration.

## Failure policy

This lives in the AI worker. `packages.llm` never imports broker code.

| Failure | Disposition | Effect |
|---|---|---|
| `IllegalStateTransitionError`, job now `DRAFTED` / `DISPATCHED` / `COMPLETED` | `ACK_DROP` | message acked, nothing billed, info log |
| `UnvalidatedDraftError` | `DEAD_LETTER` | `FatalError(reason)`; the reason says "truncated at max_tokens" when the provider's finish reason was a length stop |
| `DraftSchemaContractError`, `UnpersistableDraftError`, `IllegalStateTransitionError` for any other state, `FatalError` | `DEAD_LETTER` | job `FAILED → DEAD_LETTER`, message to `dlx.email` with `x-failure-reason` |
| anything else (`LLMTimeoutError`, `LLMResponseError`, connection errors, unknown) | `RETRY` | existing ladder, then DLQ at `max_retries` |

## Consumer core (4.13a)

`AIWorkerConsumer(BaseConsumer)` consumes one lane queue `email.<category>.<priority>`. Per job it does the following, in order:

1. Load the job (missing → `FatalError`).
2. If the job is already `DRAFTED` or later, ack without work (R19.3).
3. Load the message by `job.message_id` (missing → `FatalError`).
4. Build `Classification` from the envelope snapshot (R7.3).
5. `ContextBuilder.build_context` (`QUEUED → CONTEXT_READY`).
6. `ComplexityRouter.route`.
7. `DraftingService.draft` (`CONTEXT_READY → GENERATING → DRAFTED`).

Any exception is classified by the policy and translated: `ACK_DROP` returns, `DEAD_LETTER` raises `FatalError`, and `RETRY` re-raises. `BaseConsumer` keeps doing the transitions, ack/nack, retry publish and DLQ publish.

## Proof (acceptance)

This runs on a scratch RabbitMQ vhost with retry TTLs of 1/2/3 s, the isolated Postgres `rag_email_test`, the real consumer, and a fake model provider:

1. **Transient:** the first call times out; the message returns through `retry.return`; the job ends `DRAFTED` with exactly one draft.
2. **Permanent:** invalid output twice leads to `email.dead_letter` with a reason naming `UnvalidatedDraftError`; the job is `DEAD_LETTER`; there are no drafts; the provider was called exactly twice.
3. **Already drafted:** a redelivery is acked; nothing reaches the DLQ; there are no extra provider calls.

Unit tests cover every policy row and the truncation reason.

## Out of scope (4.13b)

- The `services/ai_worker/main.py` entrypoint and compose wiring.
- Summarizer instrumentation and token-counter warm-up.
- The composed-worker telemetry test.
- The worker-kill test.
- The `make smoke` extension.

Also out of scope: a quorum-queue delivery limit (the broker runs 3.13 with classic queues, and the ladder caps attempts), DLQ alerting (7.x), and replay endpoint changes.

## Closing 4.9

After the proof passes, a completion audit runs, as for 4.11 and 4.12. Only then do 4.9 and 4.13a flip to `[x]`.
