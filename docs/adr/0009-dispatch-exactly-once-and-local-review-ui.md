# ADR-0009: Dispatch sends exactly once through claim → provider draft → send → confirm; the review UI is local and has no login

- **Status:** Accepted
- **Date:** 2026-09-28
- **Decided by:** project owner, on the recommendation in `artifacts/superpowers/2026-09-28-phase6-dispatch-review-research.md`
- **Requirements:** R16.6–R16.8, R17.1–R17.7, R19.2, R19.3, R23.4–R23.7
- **Changes:** `specs/design.md` §5.8, §9, §12.2; `specs/tasks.md` Phase 6

## Context

Phase 6 sends AI-drafted replies through Gmail (and Microsoft Graph). Messages arrive at least once, so a dispatch job can be delivered twice.

- Design §9's `execute_once` runs the side effect inside the database transaction. That works for rows in our own database, not for a provider send: if the worker crashes after Gmail accepted the reply but before COMMIT, the redelivered job sends the customer a second email.
- Neither Gmail nor Graph accepts an idempotency key on send.
- Both providers do keep a draft as a durable object: Gmail deletes a draft automatically when it is sent, and Graph documents creating a draft with an immutable id, sending it, then fetching it by the same id to find the sent copy.
- The existing adapters had defects that break threading and retries: the Graph adapter posts new messages (`/messages`, `/sendMail`) instead of replies, the Gmail reply has no `Message-ID`, and both treat every 403 as an expired token, including Gmail's rate-limit 403s.

## Decision

1. **Dispatch in five steps, on the existing job.** The generation job that reached `DRAFTED` carries dispatch too. Step 1 claims the R19.2 dispatch key (stored `UNIQUE` on `generated_draft.dispatch_idempotency_key`) and moves the job `DRAFTED → DISPATCHED` in one transaction. Step 2 creates a provider draft and stores its id and message id. In `create_draft` mode the job then completes, with no outbound message written (the customer has received nothing). In `send_reply` mode step 3 sends the draft; after any ambiguous failure step 4 asks the provider whether it was already sent, and a draft that has disappeared counts as sent only if the provider thread holds our sent message, otherwise the job is dead-lettered for an operator. Step 5 records the result, the outbound message and `DISPATCHED → COMPLETED` in one transaction. The send never runs inside a database transaction. Transient errors keep the job `DISPATCHED` while the broker's retry ladder redelivers; a new `RETRY_PENDING → DISPATCHED` edge lets an operator replay a dead-lettered dispatch without regenerating the draft.
2. **Mode per category**, default `create_draft` for every category. `send_reply` requires an approval unless the category's `auto_send_eligible` is true, which it is for none.
3. **Adapter fixes and additions.** Graph uses `createReply` + `send` with immutable ids; Gmail sets its own `Message-ID`; both classify rate-limit 403s and 429/5xx as retryable and honour `Retry-After`. Adapters gain `send_draft` and `get_draft_status`.
4. **Review UI:** server-rendered (FastAPI + Jinja2 + htmx) in the existing `frontend` service, calling only the `/v1` API.

## Accepted scope limits

- **No login on the review UI.** It can approve and send email from the connected mailbox. Authentication and authorization are out of scope (CLAUDE.md §6), so the UI runs on a developer machine only, for demos. It must not be exposed on a network: compose binds the `frontend` and `api` ports to `127.0.0.1`, which carries out this accepted limit and adds no authentication.
- **Short-lived Gmail access tokens** are minted by hand before each demo (`docs/demo-runbook.md` §3). The adapter does not refresh tokens.
- **Graph is verified by recorded-response tests only**; the live gate uses Gmail.

## Consequences

- A crash at any step leads to exactly one provider send, which a forced-redelivery test asserts for every step.
- Categories in `create_draft` mode complete after step 2: the reply waits in the mailbox's Drafts for a person to send, and no outbound message is recorded until the provider sync sees the sent mail.
- One migration adds `generated_draft.provider_draft_id`, `provider_draft_message_id` and `dispatch_idempotency_key UNIQUE`, and `feedback.review_ms` with `UNIQUE (draft_id)`. The reviewer is a free-text label, not an identity.
- Approve commits before it publishes; a repeated approve re-publishes until the job is `COMPLETED`, so a lost publish cannot strand an approved draft.

## Alternatives rejected

- **Send, then search the mailbox for our own Message-ID on retry.** Whether Gmail keeps a caller-set Message-ID is unverified, and search is eventually consistent.
- **Keep design §9 as written.** The crash window between send and COMMIT can send a customer two emails.
- **A React single-page app** for the review UI. A second toolchain for three mostly-form screens; the `/v1` API keeps the option open.
