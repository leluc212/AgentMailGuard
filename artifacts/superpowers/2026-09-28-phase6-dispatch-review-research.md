# Phase 6 research: dispatch, threading, idempotency, review UI (2026-09-28)

Scope: primary-source research for Phase 6 of `rag-email` (tasks 6.1–6.9; R16.6–R16.8, R17.*, R23.*).
Method: every claim below cites the page that was actually opened on 2026-09-28, with a short verbatim quote.
"Date" is the page's own "last updated"/publication date where the page shows one; "undated" means none was visible in the fetched text.

Repository files read: `specs/tasks.md` (Phase 6), `specs/design.md` §5.8, §7.2, §9, `specs/requirements.md` R16.6–R16.8, R17.1–R17.7, R23.1–R23.7,
`packages/adapters/{protocol,gmail,graph}.py`, `packages/domain/entities.py` (`OutboundReply`), `packages/domain/state_machine.py`, `docker-compose.yml`.

---

## 0. The shape of Phase 6 (as the sources say it should work)

```
 inbound msg ─▶ ... ─▶ generate ─▶ DRAFTED ──────────────┐
                                                          │
            review UI / API (6.1, 6.8)                    ▼
            approve | edit | reject ──▶ feedback row (6.2: decision, edited body, edit distance, rating)
                  │
                  ▼  (auto-send only if category policy allows, 6.4)
          dispatch job on RabbitMQ (at-least-once)
                  │
                  ▼
   ┌─ claim: DB row "dispatch key" INSERT ... state=DISPATCHING (unique key) ─┐   (6.5)
   │                                                                          │
   │  1. create provider reply DRAFT  ─▶ persist provider draft/message id     │
   │     Gmail drafts.create(threadId, raw w/ In-Reply-To, References, Subject)│
   │     Graph  POST /messages/{id}/createReply  (Prefer: IdType="ImmutableId")│
   │  2. send that draft               Gmail drafts.send / Graph message: send │
   │  3. on ambiguous failure: RECONCILE before retry                          │
   │     Gmail: drafts.get → gone? (sent drafts are deleted) / rfc822msgid:    │
   │     Graph: GET message by immutable id → isDraft == false ⇒ already sent  │
   └──────────────────────────────────────────────────────────────────────────┘
                  │ success                          │ 429/5xx/timeout        │ 4xx permanent
                  ▼                                  ▼                        ▼
   persist SentRef, write outbound         retry ladder (30s/5m/30m,   dlx.email with provider
   email_message (6.7), DISPATCHED→COMPLETED   honour Retry-After)       error retained (6.6)
```

The central finding: neither Gmail nor Graph offers an idempotency key on send, so "exactly one provider send" cannot come from the provider. It has to come from (a) a durable claim row written before the side effect and (b) a check against the provider for an already-sent message before any retry. A **provider draft** gives you a stable handle for that check.

---

## 1. Email threading

### 1.1 RFC 5322 rules (Standards Track, October 2008)
Source: https://www.rfc-editor.org/rfc/rfc5322.html (dated October 2008)

- In-Reply-To: *"The "In-Reply-To:" field will contain the contents of the "Message-ID:" field of the message to which this one is a reply (the "parent message")."*
- References: *"The "References:" field will contain the contents of the parent's "References:" field (if any) followed by the contents of the parent's "Message-ID:" field (if any)."* Fallback when the parent has no References: *"...the "References:" field will contain the contents of the parent's "In-Reply-To:" field followed by the contents of the parent's "Message-ID:" field (if any)."*
- Syntax: `msg-id = [CFWS] "<" id-left "@" id-right ">" [CFWS]`. Angle brackets are part of the ID.
- Uniqueness is the generator's job: *"The uniqueness of the message identifier is guaranteed by the host that generates it."*
- Subject: *"When used in a reply, the field body MAY start with the string "Re: " ... If this is done, only one instance of the literal string "Re: " ought to be used"*.
- Recipients: the original author's mailbox, *"or mailboxes specified in the "Reply-To:" field (if it exists) MAY appear in the "To:" field of the reply"*.

### 1.2 Gmail API
- Thread criteria (Manage threads guide, updated 2026-09-10) https://developers.google.com/workspace/gmail/api/guides/threads?hl=en
  *"The requested `threadId` must be specified as part of the `drafts.message` or `messages` resource you supply with your request. The `References` and `In-Reply-To` headers must be set in compliance with the RFC 2822 standard. The `Subject` headers must match."*
- Raw format (Create and send guide, updated 2026-09-10) https://developers.google.com/workspace/gmail/api/guides/sending?hl=en
  *"Gmail messages are sent as base64URL encoded strings within the `raw` field"*; two send paths: *"send it directly using the `messages.send` method"* or *"send it from a draft, using the `drafts.send` method."*
- Drafts (Drafts guide, updated 2026-09-10) https://developers.google.com/workspace/gmail/api/guides/drafts?hl=en
  *"the drafts resource is a container that provides a stable ID because the underlying message IDs change every time the message is replaced"* and *"When the draft is sent, the draft is automatically deleted and a new message with an updated ID is created with the SENT system label. This message is returned in the drafts.send method response."*
- `drafts.send` (reference, updated 2026-04-15) https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/send?hl=en
  *"Sends the specified, existing draft to the recipients in the To, Cc, and Bcc headers."*

### 1.3 Microsoft Graph
- `createReply` (updated 2025-07-23) https://learn.microsoft.com/en-us/graph/api/message-createreply?view=graph-rest-1.0
  *"Create a draft to reply to the sender of a message in either JSON or MIME format."* JSON rule: *"Specify either a comment or the body property of the message parameter. Specifying both will return an HTTP 400 Bad Request error."* Reply-To rule: *"you should send the reply to the recipients in replyTo, and not the recipients in from."* Then *"Send the draft message in a subsequent operation."*
- `reply` (updated 2024-04-04) https://learn.microsoft.com/en-us/graph/api/message-reply?view=graph-rest-1.0
  *"Reply to the sender of a message using either JSON or MIME format."* ... *"This method saves the message in the Sent Items folder."* Response: `HTTP/1.1 202 Accepted` with no body.
- `message: send` (updated 2025-07-23) https://learn.microsoft.com/en-us/graph/api/message-send?view=graph-rest-1.0
  *"Send an existing draft message. The draft message can be a new message draft, reply draft, reply-all draft, or a forward draft."*
- `sendMail` (updated 2025-07-23) https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0
  *"If successful, this method returns `202 Accepted` response code. It doesn't return anything in the response body."* and *"A `202 Accepted` response code indicates that the request has been accepted; however, it doesn't indicate that the request processing has completed."*
- `message` resource (updated 2025-10-24) https://learn.microsoft.com/en-us/graph/api/resources/message?view=graph-rest-1.0
  `conversationId`: *"The ID of the conversation the email belongs to."* `internetMessageId`: *"The message ID in the format specified by RFC2822."* `isDraft`: *"A message is a draft if it hasn't been sent yet."* Custom headers: *"Add custom headers only when creating a message, and name them starting with "x-". After the message is sent, you cannot modify the headers."*
- Immutable IDs (updated 2024-11-07) https://learn.microsoft.com/en-us/graph/outlook-immutable-id
  *"their IDs change. It doesn't happen often, only if the item is moved"*; opt-in header *`Prefer: IdType="ImmutableId"`*; and the exact recipe for finding the sent copy: *"Create a draft message using the Prefer: IdType="ImmutableId" header and save the id property of the message in the response. Send the message using the ID from the previous step. Get the message using the ID from the first step. This is the copy in Sent Items."*

### 1.4 Quoted-original conventions
- RFC 3676 (format=flowed, February 2004) https://www.rfc-editor.org/rfc/rfc3676.html
  *"the canonical quote indicator (or quote mark) is one or more close angle bracket (">") characters. Lines which start with the quote indicator are considered quoted."*
- No provider document found that mandates a quoting style; Graph `createReply` builds the reply draft server-side from the original message, so on Graph the quote/attribution is the provider's, not ours (the doc does not describe its exact quote format — see "What this does not settle").

### 1.5 What this means for the current adapters (repo evidence)

| # | Current code | Source says | Consequence |
|---|---|---|---|
| T1 | `gmail.build_rfc822_mime` sets To/Cc/Subject/In-Reply-To/References but **no `Message-ID`** | RFC 5322: uniqueness *"guaranteed by the host that generates it"*; Gmail search supports `rfc822msgid:` (§2.3) | Without our own Message-ID we have no provider-side lookup key for duplicate detection on Gmail. |
| T2 | `OutboundReply.thread_id: UUID | str` is passed as Gmail `threadId` and Graph `conversationId` | Gmail: *"The requested threadId must be specified"* (provider's thread id) | Must be the **provider** thread id, not our internal `email_thread.id`. Worth a type split (`provider_thread_id: str`). |
| T3 | Graph `create_draft` does `POST /messages` (a new message) with `conversationId` in the body | Graph's documented reply-draft path is `createReply` | Use `POST /messages/{provider_message_id}/createReply`; the docs do not document that setting `conversationId` on a new message threads it. |
| T4 | Graph `send_reply` uses `sendMail` and returns `client-request-id` as `provider_message_id` | `sendMail` *"doesn't return anything in the response body"* | The stored "provider ref" is a request correlation id, not a message id. Use createReply → send and read the sent copy by immutable id. |
| T5 | Subject passed through as-is | Gmail: *"The Subject headers must match"*; RFC 5322: one `"Re: "` | Normalise to exactly one `Re: ` + original subject. |
| T6 | `References` built from `reply.references` | RFC 5322: parent's References + parent's Message-ID (fallback: parent's In-Reply-To) | Build from the stored parent's headers; keep angle brackets. |

---

## 2. Idempotent side effects under at-least-once delivery

### 2.1 What the broker guarantees
- RabbitMQ Reliability Guide (docs version 4.3; undated) https://www.rabbitmq.com/docs/reliability
  *"messages can be redelivered, and consumers must be prepared to handle deliveries they have seen in the past. It is recommended that consumer implementation is designed to be idempotent rather than to explicitly perform deduplication."* The redelivered flag *"is a hint that a consumer may have seen this message before. This is not guaranteed"*.
- RabbitMQ Confirms guide (4.3; undated) https://www.rabbitmq.com/docs/confirms
  *"if all consumers requeue because they cannot process a delivery due to a transient condition, they will create a requeue/redelivery loop."*
- RabbitMQ Quorum Queues (4.3; undated) https://www.rabbitmq.com/docs/quorum-queues
  *"Starting with RabbitMQ 4.0, quorum queues enforce a default delivery-limit of 20 based on delivery-count."* The 4.3 docs also list a native *"Delayed retry mechanism for message redelivery with backoff"* (`x-delayed-retry-type`, `x-delayed-retry-min`, `x-delayed-retry-max`).
  Repo note: `docker-compose.yml` pins `rabbitmq:3.13-management`, so neither the 4.x default delivery limit nor native delayed retry applies; the TTL retry ladder in design §7.2 stays the mechanism.

### 2.2 Idempotency-key patterns (first-party)
- AWS Builders' Library, "Making retries safe with idempotent APIs" (undated) https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/
  *"the process that combines recording the idempotent token and all mutating operations related to servicing the request must meet the properties for an atomic, consistent, isolated, and durable (ACID) operation."* Also: retries should get *"a semantically equivalent response in every case for the same unique request identifier"*, and a reused token with different parameters should be treated as a different intent (*"we return a validation error"*).
- Stripe API, Idempotent requests (page shows 2026-08-26) https://docs.stripe.com/api/idempotent_requests
  *"The idempotency layer compares incoming parameters to those of the original request and errors if they're not the same to prevent accidental misuse."* *"Avoid using sensitive data (for example, email addresses or personal identifiers) as idempotency keys."*
- AWS Prescriptive Guidance, Transactional outbox (undated) https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html
  *"The transactional outbox pattern resolves the dual write operations issue that occurs in distributed systems when a single operation involves both a database write operation and a message or event notification."* and *"we recommend that you make the consuming service idempotent by tracking the processed messages."*
- Azure Architecture Center, Retry pattern (undated in fetch) https://learn.microsoft.com/en-us/azure/architecture/patterns/retry
  *"a service might receive the request, process the request successfully, but fail to send a response. At that point, the retry logic might re-send the request, assuming that the first request wasn't received."*

### 2.3 Detecting "the send succeeded but our commit/ack failed"
Gmail and Graph have no request idempotency key (no idempotency header is documented on `messages.send`, `drafts.send`, `sendMail`, `reply`, or `send`). The provider-side checks the docs *do* support:

- **Gmail, draft handle:** a sent draft *"is automatically deleted and a new message ... is created with the SENT system label"* (Drafts guide above). So: persist the `draft.id` before sending; after an ambiguous failure, `drafts.get(id)` returning not-found means it was sent (or deleted by the user — resolve with the search below).
- **Gmail, header search:** `users.messages.list` (reference, updated 2026-04-15) https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list?hl=en
  `q`: *"Supports the same query format as the Gmail search box. For example, "from:someuser@example.com rfc822msgid:<somemsgid@example.com> is:unread"."* and *"Parameter cannot be used when accessing the api using the gmail.metadata scope."*
  Gmail Help, search operators (undated) https://support.google.com/mail/answer/7190?hl=en: `rfc822msgid` — *"Find emails with a specific message-id header."*
  This works only if we set a deterministic `Message-ID` (for example derived from the dispatch key) and Gmail keeps it on send — the second part is **not documented** (see §8).
- **Graph, immutable id:** the documented recipe in §1.3 (*"Get the message using the ID from the first step. This is the copy in Sent Items."*) plus `isDraft` (*"A message is a draft if it hasn't been sent yet"*) turns "did it send?" into one GET.
- **Graph, custom header:** custom `x-` headers can be set on create (*"name them starting with "x-""*), which lets you stamp the dispatch key on the message itself for audit.

### 2.4 Why design §9's `execute_once` is not enough for dispatch
Design §9 wraps `op()` and `mark_completed` in one DB unit of work. AWS's rule is that the token and *all* mutations must be one ACID operation, and a provider send cannot join a Postgres transaction. The rollback removes the DB record but not the sent email. Result: a crash after the provider accepts and before COMMIT leaves no "completed" row, redelivery runs `op()` again, and the customer gets two emails. The Azure retry-pattern quote above describes exactly this case.

Pattern that fits the sources (claim → side effect → reconcile):

```
tx1: INSERT dispatch(key UNIQUE, state='CLAIMED', attempt) ─ conflict? ─▶ load row, go to its state
     create provider draft  ─▶ tx2: state='DRAFT_CREATED', provider_draft_id, message_id_header
     send draft             ─▶ tx3: state='SENT', provider_message_id  ─▶ write-back 6.7 ─▶ COMPLETED
redelivery / timeout in any step:
     state=DRAFT_CREATED ─▶ ask provider: draft still unsent?  yes → send it   no → mark SENT (fetch sent copy)
     state=SENT          ─▶ skip send; only redo write-back + ack
```

This is the outbox idea turned around (the DB row is the durable intent, written before the external call). The provider draft is the idempotency handle the APIs don't give us. It also matches the R17.1 `create_draft` mode, because `create_draft` is just step 1 without step 2.

---

## 3. Retry classification and backoff

| Signal | Gmail (Resolve errors, updated 2026-09-15) | Graph (errors 2025-08-06, throttling 2025-01-15) | Class |
|---|---|---|---|
| 429 | *"can occur due to daily per-user limits (including mail sending limits), bandwidth limits, or a per-user concurrent request limit"*; fix by *"retrying failed requests"* | *"use the HTTP error code 429 to detect throttling. The failed response includes the `Retry-After` response header."* | transient, honour Retry-After |
| 403 `rateLimitExceeded` / `userRateLimitExceeded` | *"Use exponential backoff to retry the request."* | — | **transient** (rate limit) |
| 403 `dailyLimitExceeded` | *"raise the quota in the Google Cloud project"* | — | permanent for the day (DLQ or park) |
| 500/502/503/504 | `backendError`: *"use exponential backoff to retry the request."* | 503: *"You can repeat the request after a delay, the length of which can be specified in a Retry-After header."* | transient |
| 400 | *"The server couldn't fulfill the request due to a client error."* | *"Can't process the request because it's malformed or incorrect."* | permanent → DLQ with body |
| 401 | *"the access token you're using is either expired or invalid"* | — | refresh once, then permanent |
| timeout / connection reset on a **send** | — | Azure: *"process the request successfully, but fail to send a response"* | **ambiguous**: reconcile (§2.3) before retrying |

Sources: https://developers.google.com/workspace/gmail/api/guides/handle-errors?hl=en ; https://learn.microsoft.com/en-us/graph/errors ; https://learn.microsoft.com/en-us/graph/throttling (also: *"If no `Retry-After` header is provided by the response, we recommend implementing an exponential backoff retry policy."*)

Backoff with jitter:
- Google Cloud Storage retry strategy (updated 2026-09-24) https://cloud.google.com/storage/docs/retry-strategy — *"The following responses indicate transient problems that are useful to retry: HTTP 408, 429, and 5xx response codes. Socket timeouts and TCP disconnects."* and *"You should generally use exponential backoff with jitter to retry requests that meet both the response and idempotency criteria."*
- AWS Architecture Blog, "Exponential Backoff And Jitter" (04 Mar 2015, updated May 2023) https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/ — *"Most AWS SDKs now support exponential backoff and jitter as part of their retry behavior"*; compares "Full Jitter", "Equal Jitter", "Decorrelated Jitter".
- Gmail quota (updated 2026-09-10) https://developers.google.com/workspace/gmail/api/reference/quota?hl=en — `messages.send` costs 100 units; *"Per minute per user per project 6,000 quota units"*.

**Repo gap (adapter error mapping):** both `_request` implementations map `401, 403 → AuthExpired`. Per the Gmail doc, 403 `rateLimitExceeded`/`userRateLimitExceeded` is a rate limit, not an auth failure, so the adapter has to read `error.errors[].reason`. Both adapters also raise `Transient` on transport errors. That is right for reads, but for a send it has to trigger reconciliation, not a blind retry.

---

## 4. Human-in-the-loop review of AI-drafted email

### 4.1 Vendor practice (approval gates, edit, sources, narrow auto-actions)
- Zendesk "About auto assist" (undated) https://support.zendesk.com/hc/en-us/articles/9945148867866-About-auto-assist — *"Agents review and approve suggestions before sending them"*; *"agents are always in control of the replies they send to customers"*; narrow auto-execution: *"Some actions, such as checking an order status, may be performed automatically by auto assist if the action was pre-approved by you or another admin in a procedure. Actions that are performed automatically are logged in the ticket's events"*.
- Zendesk "Using auto assist to solve tickets" (undated) https://support.zendesk.com/hc/en-us/articles/7051314237466-Using-auto-assist-to-solve-tickets — *"You can review, edit, approve, or dismiss suggestions"*; *"If you have permission, you can also view the source used to generate the suggestion"*; *"Any replies sent and any actions taken are performed under your name."*
- Intercom "How to use Copilot" (undated) https://www.intercom.com/help/en/articles/8587194-how-to-use-copilot — *"Below the direct answer, Copilot will surface the relevant sources used."*; *"If you're happy with the answer, you can click on Add to composer"*; macro suggestions: *"hit Tab to insert the macro and then click Send, or press Esc to reject the suggestion."*
- Microsoft Support, "Draft an email message with Copilot in Outlook" (undated) https://support.microsoft.com/en-us/outlook/copilot-pages/draft-an-email-message-with-copilot-in-outlook — *"Copilot drafts a message for you. Review the message."* ... *"When you're satisfied with the result, select Keep it ... Edit the draft as needed, and then select Send."*
- Microsoft Learn, Responsible AI FAQ for Copilot for email (Dynamics 365 Sales, updated 2025-11-12) https://learn.microsoft.com/en-us/dynamics365/sales/faqs-sales-copilot-for-email — *"provides a summary and draft that the seller can review before responding to their customer."*
- Front "Copilot" (edited Thursday, September 24 2026) https://help.front.com/en/articles/2344960 — the page's contents include "Using Copilot to draft replies"; nothing quotable was extracted about approval semantics (JS-rendered body).

Common pattern: the draft goes into the human's composer, sources can be shown next to it, the send happens under the human's name, and anything automatic is pre-approved by an admin, narrowly scoped, and logged. This backs R16.8 (HITL default) and R17.6 (per-category opt-in auto-send, logged).

### 4.2 Measuring acceptance and edit distance
- GitHub Copilot usage metrics (undated) https://docs.github.com/en/copilot/reference/copilot-usage-metrics/copilot-usage-metrics — *"Code completion acceptance rate: Percentage of suggestions accepted by users."* This is the closest first-party precedent for "draft acceptance rate" = accepted / shown.
- No vendor doc found that defines an edit-distance metric for email drafts. Recommendation (engineering, not sourced): store the raw generated body and the sent body, compute character-level Levenshtein and normalised distance (distance / max(len)), and count "approved unedited", "approved with edits", "rejected" as separate outcomes, so "accepted" does not hide heavy rewrites.

### 4.3 Automation bias / over-reliance evidence and mitigations
- Goddard, Roudsari, Wyatt, "Automation bias: a systematic review..." (JAMIA 2012) https://pmc.ncbi.nlm.nih.gov/articles/PMC3240751/ — *"Automation bias (AB)—the tendency to over-rely on automation"*; mediators include *"workload, task complexity, and time constraint"*; mitigators include *"emphasizing user accountability"* and *"the position of advice on the screen, updated confidence levels attached to DSS output"*. Also: *"Automation complacency error rates ... increase if a DSS is highly (but not perfectly) reliable"*.
- Lee et al., CHI 2025 (Microsoft Research, April 2025) https://www.microsoft.com/en-us/research/publication/the-impact-of-generative-ai-on-critical-thinking-self-reported-reductions-in-cognitive-effort-and-confidence-effects-from-a-survey-of-knowledge-workers/ — 319 knowledge workers; *"a user's task-specific self-confidence and confidence in GenAI are predictive of whether critical thinking is enacted and the effort of doing so in GenAI-assisted tasks."*
- Vasconcelos et al., "Explanations Can Reduce Overreliance on AI Systems During Decision-Making" (arXiv 2212.06823, submitted 13 Dec 2022; CSCW 2023) https://arxiv.org/abs/2212.06823 — *"overreliance, when people agree with an AI, even when it is incorrect. Surprisingly, overreliance does not reduce when the AI produces explanations"*; the paper argues people *"strategically choose whether or not to engage with an AI explanation"*. Verification aids have to be cheap to use.
- Microsoft Learn, "Overreliance on AI: Risk identification and mitigation framework" (updated 2025-03-04) https://learn.microsoft.com/en-us/ai/playbook/technology-guidance/overreliance-on-ai/overreliance-on-ai — goals *"Signal to users when to verify: Make it easy for users to spot AI mistakes. Facilitate verification: Decrease users' cognitive load when verifying AI outputs."* Mitigations: *"Cognitive forcing functions (friction, giving users time to think, confirmation dialogues...)"*. Warning: *"Explanations can increase user trust even when they are incorrect. The mere presence of sources can make users trust AI outputs more."*

UI implications for 6.8 (derived from the above):
1. Put the original email and the draft side by side, and show **cited chunks inline with the claim they support**, not in a collapsed footer (Microsoft: lower verification cost).
2. **Highlight business facts** in the draft (order ids, amounts, dates, statuses from the Phase 5 business context) and show where each came from. These are the facts a reviewer must check. Flag facts with no citation.
3. Show the confidence/guard signals the pipeline already has (triage confidence, citation guard result) next to Approve.
4. Add light friction only where risk is high. For example, "Approve & send" on a draft with an uncited business fact or a low-confidence category needs a second confirm. Don't put friction everywhere (Microsoft: *"many overreliance mitigations can backfire"*).
5. Log the reviewer and the time spent on each decision (Goddard: accountability is a mitigator). The dwell-time metric can later show rubber-stamping (approvals in seconds, zero edits).

---

## 5. Review UI stack and accessibility

- FastAPI Templates (undated) https://fastapi.tiangolo.com/advanced/templates/ — *"Use the templates you created to render and return a `TemplateResponse`, pass the name of the template, the request object, and a "context" dictionary"*; FastAPI ships `Jinja2Templates` and `StaticFiles`, so no new service is needed.
- htmx docs (undated) https://htmx.org/docs/ — `<button hx-post="/clicked" hx-trigger="click" hx-target="#parent-div" hx-swap="outerHTML">` means *"When a user clicks on this button, issue an HTTP POST request to '/clicked' and use the content from the response..."*
- htmx essay "When should you use hypermedia?" (undated) https://htmx.org/essays/when-to-use-hypermedia/ — good fit *"If your UI is mostly text & images"*; not a good fit *"If your UI has many, dynamic interdependencies"*.
- React docs (React v19.3; undated) https://react.dev/learn/creating-a-react-app — *"If you want to build a new app or website with React, we recommend starting with a framework."* A plain Vite SPA is the "from scratch" path. Either way it adds a Node toolchain and a second deployable to a Python-only repo.
- WCAG 2.2 (W3C Recommendation 12 December 2024) https://www.w3.org/TR/WCAG22/ — new in 2.2: *"2.4.11 Focus Not Obscured (Minimum) (AA) ... 2.5.7 Dragging Movements (AA) 2.5.8 Target Size (Minimum) (AA) 3.2.6 Consistent Help (A) 3.3.7 Redundant Entry (A) 3.3.8 Accessible Authentication (Minimum) (AA)"*.

Assessment: the review screens (queue list, detail with text panels, three buttons, timeline list, upload form with status) are "mostly text", with few cross-component dependencies. FastAPI + Jinja2 + htmx fits the htmx essay's "good fit" case and keeps one Python deployable. Accessibility basics that matter here: keyboard-reachable approve/edit/reject with visible, unobscured focus (2.4.7/2.4.11); targets at least 24×24 CSS px (2.5.8); real `<form>` labels on the edit textarea and rating; announce htmx swaps (ingestion status, "sent") through a live region (4.1.3 Status Messages); don't make the reviewer re-type data already shown (3.3.7).

---

## 6. Security controls to FLAG (out of scope per GEMINI.md §6 — not designed here)

- **Reviewer authn/authz and org scoping.** R23.6 requires `organization_id` on every request. Without authentication, anyone who can reach the UI can approve sends for any org. (OWASP Top 10:2025 A01 "Broken Access Control": https://owasp.org/Top10/2025/A01_2025-Broken_Access_Control/, page opened; category title only.)
- **Sending as the user.** Zendesk: *"Any replies sent and any actions taken are performed under your name."* Our system sends from the connected mailbox, so every approval is an act in that person's name. Keep a reviewer identity in `feedback` even without auth.
- **OAuth scope breadth.** Gmail scopes (updated 2026-09-10) https://developers.google.com/workspace/gmail/api/auth/scopes?hl=en — `gmail.compose`: *"Manage drafts and send emails."*; `gmail.send`: *"Send email on your behalf."*; `https://mail.google.com/` is listed under *"Restricted scopes"*. The draft-then-send design needs `gmail.compose`.
- **Idempotency key content.** Stripe: *"Avoid using sensitive data (for example, email addresses or personal identifiers) as idempotency keys."* Design §9 hashes the key with sha256, which is consistent; a deterministic `Message-ID` should also be hashed, not built from addresses.
- **CSRF on state-changing UI posts** (approve/send via htmx `hx-post`). This is a web-security control. Flagged only.
- **Auto-send is a risk policy.** Per-category auto-send widens what can leave the building without a human. Flagged; the governance design is out of scope.

---

## 7. Recommendations mapped to Phase 6 tasks

| Task | Recommendation | Grounding |
|---|---|---|
| **6.1 Draft API** | `/v1/drafts` list (cursor/limit, filters status/category/mailbox, `organization_id` required), `GET /{id}`, `PATCH /{id}` (edit body), `POST /{id}:approve`, `POST /{id}:reject`. Approve takes an optional `mode` only if category policy allows `send`. Return the cited chunks and business facts with the draft so the UI needs no second fetch. | R16.6, R23.6; Zendesk/Intercom sources-with-draft pattern (§4.1) |
| **6.2 Feedback** | One `feedback` row per decision: `decision ∈ {approved_unedited, approved_edited, rejected}`, `generated_body`, `final_body`, `edit_distance` (Levenshtein) + `edit_ratio`, `rating`, `reviewer`, `decision_ms` (dwell). Metric `draft_acceptance_rate = approved / (approved+rejected)`, plus a separate `approved_unedited_rate` so heavy edits don't count as clean acceptance. | GitHub acceptance-rate definition; Goddard (accountability); Microsoft (verify signals) |
| **6.3 OutboundReply** | Split `thread_id` into internal id vs `provider_thread_id: str` (T2). Derive `In-Reply-To` = parent `Message-ID`, `References` = parent References + parent Message-ID (RFC fallback) (T6). Subject = one `Re: ` + original (T5). Reply to `Reply-To` if present (RFC 5322, Graph createReply). Set a deterministic `Message-ID` `<sha256(dispatch_key)@<our-domain>>` (T1). Plain-text quote with `> ` per RFC 3676 on Gmail; on Graph let `createReply` build the reply. | §1 |
| **6.4 Modes** | Implement both modes on a **draft-first** path: `create_draft` = Gmail `drafts.create` / Graph `createReply`; `send_reply` = the same, then Gmail `drafts.send` / Graph `message: send`. Fix Graph `create_draft` (T3) and `send_reply` (T4). Default category policy `draft_only`; `auto_send` is explicit per category, and every auto-send is logged like Zendesk's pre-approved actions. | §1.2–1.3, §4.1 |
| **6.5 Idempotency** | Replace the "side effect inside txn" shape for dispatch with the claim → draft → send → reconcile state machine (§2.4). Unique `dispatch.idempotency_key`; the reconcile step asks the provider (Gmail draft gone / `rfc822msgid:`; Graph immutable-id GET + `isDraft`). The forced-redelivery test should also cover "crash after provider send, before commit" against the fake adapter, and the fake must model draft deletion on send. | AWS ACID rule, Azure retry quote, Gmail drafts, Graph immutable ID |
| **6.6 Failure** | Classify by status **and** reason (§3 table). Fix the `403 → AuthExpired` mapping for Gmail rate-limit reasons. Honour `Retry-After`, otherwise the existing 30s/5m/30m ladder (add jitter if the ladder moves in-process). Treat transport timeouts on send as ambiguous: reconcile first. Permanent → `dlx.email` with provider status, reason and body in `x-failure-reason`. | Gmail/Graph error docs, GCS, AWS jitter |
| **6.7 Write-back** | After SENT, fetch the sent copy (Gmail: `drafts.send` response message; Graph: GET by immutable id) and insert it as `email_message(direction='outbound')` with the real provider id and `internetMessageId`, so the next inbound reply's `In-Reply-To` matches a stored row. Make the insert idempotent on `(mailbox, provider_message_id)`, because the sync loop will also see the sent message. | Gmail drafts guide; Graph immutable-id recipe |
| **6.8 UI** | FastAPI + Jinja2 + htmx in the API process. Queue → detail (original, thread summary, draft editor, cited chunks inline, business facts highlighted) → approve/edit/reject; job timeline from `job_event`; knowledge upload with a polled status fragment in a live region. Meet the WCAG 2.2 AA items in §5. | §4.3, §5 |
| **6.9 E2E smoke** | Fixture email → … → approve → dispatch against the fake adapter, asserting: one `provider.send` call, threading headers present and RFC-shaped, outbound row written, replayed dispatch job makes zero provider calls. | R24.7; Phase 6 gate |

---

## 8. What this does not settle

1. **Does Gmail keep a client-supplied `Message-ID` on `messages.send`/`drafts.send`?** No Google page opened today says so either way. The `rfc822msgid:` dedupe only works if it does. Verify with one live send. The draft-deletion check does not depend on it.
2. **Does Graph `createReply` (JSON) quote the original body, and in what format?** Not described in the page opened. Check with one live call.
3. **Can a user-deleted Gmail draft be told apart from a sent draft?** Both make `drafts.get` fail. The fallback is a Sent search (`in:sent rfc822msgid:`), which depends on item 1.
4. **Graph immutable IDs across archive/export.** The doc says the immutable id changes if the item is moved *"to an archive mailbox"* or exported and re-imported. That's an edge case, not handled here.
5. **Edit-distance definition.** No primary source defines the right metric for email drafts. Character vs token Levenshtein, and normalisation, are our choice. Record it in an ADR.
6. **Measured automation bias in this system.** The evidence is from clinical DSS and knowledge-worker surveys, not email-draft review. Whether our reviewers rubber-stamp is only visible in our own dwell-time and edit data.
7. **Graph `conversationId` writability.** The resource page describes it but the fetched text did not show whether it is read-only on create. The recommendation (use `createReply`) avoids depending on it.
8. **Vendor pages without dates** (Zendesk, Intercom, Microsoft Support, AWS Builders' Library, htmx, FastAPI) may change without notice.
9. **RabbitMQ version.** The 4.x features cited (default delivery-limit 20, native delayed retry) do not apply to the pinned `rabbitmq:3.13`. Whether to upgrade is not a research question.

---

## Plain-terms summary

> **Problem:** Gmail and Outlook give no "send exactly once" switch, and the current design commits the database *around* the send, so a crash at the wrong moment sends the customer two emails. The current Graph adapter also sends a brand-new email, not a threaded reply, and stores a request id as if it were the message id.
> **Need you to:** decide whether Phase 6 adopts the "create a provider draft first, then send it" approach (it covers both dispatch modes and gives a way to check "did it already go out?"), and pick the review UI stack (server-rendered FastAPI+htmx is the smaller option). Two facts need one live test each with a real mailbox (§8 items 1–2). I can't run those against your accounts.
