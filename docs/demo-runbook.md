# Demo runbook

How to bring the rag-email stack up for a live demo, from a fresh clone to a reply in a real Gmail thread. Every step is a command you can copy.

- **Who this is for:** the project owner, or an examiner reproducing the demo on their own machine.
- **Last verified:** 2026-09-28 at commit `d229fa3` (branch `RAG_Email_System`), through §5.2.
- **Is Phase 6 built?** Steps marked **[after Phase 6]** need Phase 6 (sending replies and the review UI). Check with `grep -c 'phase6-gate' Makefile`: `0` means Phase 6 is not built yet, so skip every **[after Phase 6]** step. Everything else works now.
- **Time:** first-time setup about 30 minutes (plus 15 minutes for §3 after Phase 6); a warm-up on an already-set-up machine about 10 minutes; the demo itself about 10 minutes.

**What you need:** Linux or macOS with Docker (with Compose v2), `make`, `git`, [`uv`](https://docs.astral.sh/uv/) (it runs the project's Python scripts on your machine), and a Google account for the Gemini API key. **[after Phase 6]** also a Gmail test account (§3.1).

**Keep secrets off screen.** Do §2 and §3.2 before the audience arrives. Never open `.env`, AI Studio's key page or the OAuth Playground on a projected or recorded screen: they show your keys and tokens.

### Glossary

| Term | Meaning |
|---|---|
| tenant | One customer organisation in the system. The demo data has three; they must never see each other's data. |
| ai-worker | The service that builds the context for an email and makes the one model call that writes the draft. |
| `DRAFTED`, `COMPLETED` | Job states: a draft exists; the whole job finished (after Phase 6: the reply was dispatched). |
| dead-lettered | Moved to an error queue instead of being processed, e.g. a request that crosses tenants. |
| seeded | Loaded by `make seed`: demo tenants, customers (such as Alice), orders, knowledge documents and fixture emails. |
| fake model | A built-in offline stand-in for the LLM. It makes no API calls and is what the automated tests use. |
| gate | A script that runs one scenario through the live stack and checks the result, ending in a line with `OK`. |
| review UI **[after Phase 6]** | A local web page (no login, local use only) listing pending drafts to approve, edit or reject. |

---

## 1. Pre-demo checklist

Tick these off in order before an audience arrives.

- [ ] The repo is cloned on branch `RAG_Email_System` (§2.0).
- [ ] `.env` has the Gemini settings (§2.2).
- [ ] `make llm-smoke` prints `LLM SMOKE OK` (§2.3).
- [ ] **[after Phase 6]** A Gmail access token less than 45 minutes old is in `.env` (§3.2–3.3). It expires after about an hour.
- [ ] `make up` finished and the containers are healthy (§4).
- [ ] `make seed` ran (§4).
- [ ] `make phase5-gate` prints `PHASE 5 GATE OK` (§5.1). This warm-up proves the model, database and queues work. Leave at least 2 minutes before the live run to stay clear of the API's rate limit.
- [ ] Optional, on the day before rather than the warm-up: the regression checks in §5.2 pass. With the pauses between runs they add about 10 minutes.
- [ ] **[after Phase 6]** Browser tabs open: the Gmail test inbox and the review UI at `http://localhost:3001`.

---

## 2. Model setup (Google Gemini API)

The stack writes replies with Google's Gemma and Gemini models through the Gemini API's OpenAI-compatible endpoint. No local models are needed.

### 2.0 Get the code (once)

```bash
git clone https://github.com/leluc212/AgentMailGuard.git
cd AgentMailGuard
git checkout RAG_Email_System
```

All commands below run from this repository root.

### 2.1 Get a Gemini API key

1. Open [Google AI Studio](https://aistudio.google.com/) and sign in with any Google account.
2. Create an API key (menu labels as of September 2026: **Get API key → Create API key**).
3. Keep the key private: never commit it, never paste it into a chat, slide or document.

### 2.2 Put the key in `.env`

```bash
cp .env.example .env      # only if .env does not exist yet
```

`.env.example` already has most of these settings with other values. Change those lines, and add any that are missing, so `.env` contains these lines with exactly these values (keep every other line of `.env` as it is):

```bash
LLM__PROVIDER=openai
LLM__OPENAI_API_KEY=<your Gemini API key>
LLM__OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM__FAST_MODEL=gemma-4-26b-a4b-it
LLM__STRONG_MODEL=gemma-4-31b-it
LLM__FALLBACK_MODEL=gemini-3.1-flash-lite
LLM__PRICE_TABLE={"gemma-4-26b-a4b-it":{"input_per_m":0,"output_per_m":0},"gemma-4-31b-it":{"input_per_m":0,"output_per_m":0},"gemini-3.1-flash-lite":{"input_per_m":0.25,"output_per_m":1.50},"text-embedding-3-small":{"input_per_m":0.02,"output_per_m":0}}
EMBEDDING__MOCK=true
```

- The three model lines: routine drafts use the free Gemma 26B model, harder emails escalate to the free Gemma 31B model, and the paid Flash-Lite model is only a fallback.
- `LLM__PRICE_TABLE` lets the stack record the cost of each call (USD per million tokens).
- `EMBEDDING__MOCK=true` uses a built-in offline embedder for knowledge search, so no embedding API key is needed. Search still runs end to end; the embeddings are simply not semantic.

`.env` is ignored by git, so it stays on your machine. `docs/configuration.md` §2.6 documents every setting.

### 2.3 Check the model works

```bash
make llm-smoke
```

This runs on your machine through `uv` and does not need the stack to be running. Expected: two `ok` lines (one triage call, one draft call, both on `gemma-4-26b-a4b-it`) and `LLM SMOKE OK`. It does not exercise the fallback model, so it costs nothing.

---

## 3. Gmail setup **[after Phase 6]**

The live demo sends a real email to a Gmail inbox that the stack watches, and replies into the same thread. The Gmail adapter needs an OAuth **access token**. It cannot refresh tokens itself, so you mint a short-lived one before each demo.

### 3.1 Create a dedicated Gmail test account (once)

Use a separate account, never your personal mailbox: the stack can create drafts and send email from it.

1. Create a new Google account at [accounts.google.com/signup](https://accounts.google.com/signup), for example `ragemail.demo.<yourname>@gmail.com`.
2. Sign in once and send one test email to it from another address, so the inbox is not empty.

### 3.2 Mint an access token in the OAuth 2.0 Playground

**If you want to use your own Google Cloud OAuth client, do Appendix A first.** Otherwise the Playground's built-in client is fine.

Do this off screen, in a browser window signed in **only** to the test account (a private window is easiest).

1. Open the [OAuth 2.0 Playground](https://developers.google.com/oauthplayground/).
2. In **Step 1 — Select & authorize APIs**, type this scope into the box at the bottom and press **Authorize APIs**:

   ```
   https://www.googleapis.com/auth/gmail.modify
   ```

   Google describes `gmail.modify` as "Read, compose, and send emails from your Gmail account", without permanent deletion. One scope covers everything the demo does: watching the inbox, creating drafts and sending replies.
3. Sign in with the **test account** and allow access. Google may warn that the app is not verified; continue only because this is your own test account.
4. In **Step 2**, press **Exchange authorization code for tokens**.
5. Copy only the **Access token** (it starts with `ya29.`). The Playground shows how long it stays valid, about an hour. It also shows a **refresh token**: do not copy or save it. The stack does not use it, and §8 revokes it.

### 3.3 Put the token in `.env`

```bash
GMAIL_ACCESS_TOKEN=ya29.<rest of the token>
```

Then run `make up` (§4) so the containers pick it up. When the token expires you will see HTTP 401 errors (§7): repeat §3.2–3.3.

### 3.4 Connect the mailbox to the stack

Run this once after `make up` and `make seed` (§4), and again whenever you want the stack to start watching "from now":

```bash
make connect-gmail ADDRESS=ragemail.demo.<yourname>@gmail.com
```

Expected:

```
ok   token belongs to ragemail.demo.<yourname>@gmail.com; current historyId 1234567
ok   mailbox <id> in organization 00000000-0000-0000-0000-000000000001: provider gmail, credentials_ref env:GMAIL_ACCESS_TOKEN, status active, watching from now
CONNECT GMAIL OK mailbox_id=<id>
```

- It checks that the token in `.env` belongs to that address, so a token minted while signed in to another account is refused.
- The mailbox joins the demo tenant (Acme), so the seeded customers, orders and knowledge apply to its emails.
- Only mail that arrives after this command is imported. Older mail in the test inbox is left alone.
- Copy the mailbox id: §6 step 4 uses it. Nothing pushes new mail to a stack on `localhost`, so you pull it with one command (§6 step 4); `make phase6-gate` pulls it for you.
- If Gmail calls failed with 401, the stack marks the mailbox `needs_reauth`. After minting a new token (§3.2–3.3) and `make up`, run this command again to re-activate it.

---

## 4. Start the stack and load demo data

```bash
make up      # builds images and starts every service
make seed    # loads the demo tenants, customers, orders, knowledge and fixture emails
```

The first `make up` takes several minutes. Later runs reuse the built images and take under a minute unless the code changed. `make seed` is safe to repeat; it updates the demo records in place.

Check the services:

```bash
docker compose ps --format '{{.Service}} {{.Status}}'
```

Expected: these services show `Up … (healthy)`: `api`, `frontend`, `mail-connector`, `email-worker`, `triage-worker`, `ai-worker`, `knowledge-worker`, `dispatch-worker`, `postgres`, `rabbitmq`, `minio`, `prometheus`, `grafana`. `init` is not listed: it runs the database migrations and then exits, which is correct.

---

## 5. Live checks

Run these in order. Each prints one line per check and ends with a line ending in `OK`.

### 5.1 Phase 5 gate: knowledge vs live business data

```bash
make phase5-gate
```

Expected: `PHASE 5 GATE OK`. The gate emails "What is the status of order 82915?" from the seeded customer Alice, waits for the draft and prints it.

The gate checks automatically that the draft contains the word `dispatched`, cites the order-status procedure, and has no citation that points at text the model was not given. It checks for the word, not the meaning, so a draft saying "has **not** been dispatched" would also pass. **Read the printed draft yourself.** A correct draft looks like:

> Our records indicate that order ORD-82915, placed on 2026-09-28, has been dispatched. Please note that automated courier tracking numbers are provided once an order is packed at our central warehouse, and tracking links typically become active within 12 hours [060bc50c-…-01].

The first sentence comes from the business tables. The second comes from the knowledge base; the bracketed id at its end is the citation.

### 5.2 Regression checks

```bash
make smoke             # SMOKE OK: billing email drafted, newsletter skipped, cross-tenant request dead-lettered
make retrieval-gate    # RETRIEVAL GATE OK: knowledge search and query embedding work
make phase4-gate       # PHASE 4 GATE (default) OK: a 12-message thread is summarised and drafted
```

`phase4-gate` has other modes for testing model routing; the default mode is enough for a demo. These make real model calls: run them before the demo, not during it.

### 5.3 Phase 6 gate **[after Phase 6]**

The gate sends a real reply from the test account, so it needs the billing category set to send directly. Do this only for the gate:

1. In `config/categories.yaml`, under `- category: billing`, set `dispatch_mode: send_reply`.
2. Run `make up`, which rebuilds the image with the changed file.
3. Run:

   ```bash
   make phase6-gate
   ```

4. When it prints `>>> From ANOTHER address, send an email to … now.`, send that email exactly, from an address other than the test account. The subject carries a code like `[gate-1a2b3c4d]` that the gate looks for. You have 10 minutes.

Expected, ending in `PHASE 6 GATE OK`:

```
ok   GMAIL_ACCESS_TOKEN is set (not printed)
ok   config/categories.yaml: billing dispatch_mode send_reply
ok   api ready; consumers on mail.sync.requested, email.normalize, email.triage, knowledge.ingest
ok   consumer on email.dispatch
ok   connected mailbox ragemail.demo.<yourname>@gmail.com (<id>)
ok   email <gmail id> ingested into our thread
ok   job DRAFTED (billing); draft <id> listed, readable, send_reply
ok   approved -> COMPLETED; one outbound email_message replies to the original
ok   Gmail thread <gmail thread id> holds the threaded reply
ok   replayed dispatch + repeated approve: nothing sent (Gmail thread 2 messages, job COMPLETED)
PHASE 6 GATE OK
```

Your sending address receives the reply in the same conversation. Afterwards, set billing back to `dispatch_mode: create_draft` and run `make up`, so the demo (§6) creates drafts, as it does by default.

If it prints `FAIL …`, the message names the check. `FAIL Gmail: … 401` means the token expired: repeat §3.2–3.3, `make up`, `make connect-gmail`, then rerun.

---

## 6. Demo script (what to show, in order)

About 10 minutes. Steps 4–7 need Phase 6 (see the check at the top) and a connected mailbox (§3.4).

1. **The problem (1 min).** One email: "What is the status of order 82915?" A plain retrieval system would answer from documents, but the right answer is live data.
2. **Run `make phase5-gate` (2 min).** While it runs, explain the pipeline: the email is cleaned up, classified, queued, then the ai-worker builds its context and makes one model call.
3. **Read the draft aloud (1 min).** Point out the two sources: "has been dispatched" came from the business tables; the tracking-link sentence came from the knowledge base and carries a citation.
4. **[after Phase 6] Send a real email (1 min).** From another address, email the Gmail test account asking about an order (for example Alice's "What is the status of order 82915?"; send it from `alice.smith@clientcorp.com` only if you control that address, otherwise the sender is an unknown customer and the draft says so). Then pull it into the stack, with the mailbox id from §3.4:

   ```bash
   curl -s -X POST -H 'X-Organization-ID: 00000000-0000-0000-0000-000000000001' \
     -H 'Content-Type: application/json' -d '{}' \
     http://localhost:8000/v1/mailboxes/<mailbox id>/resync
   ```

   Expected: HTTP 202 and a JSON body with `"status": "enqueued"`. The draft appears in the review UI within about a minute.
5. **[after Phase 6] Review it (2 min).** In the review UI at `http://localhost:3001`, open the pending draft. Show the original email, the cited knowledge and the business facts, edit one sentence, and approve.
6. **[after Phase 6] Show the reply (1 min).** In Gmail, show the reply in the same thread. By default the stack creates a Gmail **draft** reply, which you send from Gmail; categories configured for direct sending send it straight away.
7. **[after Phase 6] Show the timeline (1 min).** In the review UI, open the message's job timeline from arrival to `COMPLETED`.

Keep the draft printed by the warm-up `make phase5-gate` in a terminal as a fallback if the network or Gmail misbehaves.

---

## 7. Troubleshooting

Any fix that touches `.env`, AI Studio or the OAuth Playground: switch the projector off or share a different window first.

| Symptom | Likely cause | Fix |
|---|---|---|
| `make llm-smoke` says `LLM__PROVIDER is fake` | `.env` lacks `LLM__PROVIDER=openai` | Add it (§2.2) |
| Services refuse to start: a Gemini or Gemma model at the OpenAI URL | `LLM__OPENAI_BASE_URL` is missing or blank | Set it to the Gemini URL (§2.2), then `make up` |
| A model call fails with HTTP 429 | The Gemini API's rate limit for your key | Wait a minute and rerun; leave a couple of minutes between gate runs |
| A model call fails with HTTP 401 or 403 | Wrong or revoked Gemini key | Create a new key in AI Studio, update `.env`, `make up` |
| **[after Phase 6]** Gmail calls fail with HTTP 401 | The Gmail access token expired (about an hour) | Mint a new one (§3.2), update `.env`, `make up` (under a minute) |
| **[after Phase 6]** `make connect-gmail` says the token belongs to another account | The Playground was signed in to a different Google account | Mint the token in a private window signed in only to the test account (§3.2) |
| **[after Phase 6]** Resync returns 409, or the gate says `needs_reauth` | A 401 marked the mailbox for re-authentication | New token (§3.2–3.3), `make up`, then `make connect-gmail ADDRESS=…` again |
| **[after Phase 6]** An approved draft ends `DEAD_LETTER` | A permanent provider error (expired token, the email's thread was deleted in Gmail) | `curl -H 'X-Organization-ID: …' http://localhost:8000/v1/jobs/<job id>` shows `last_error`; fix the cause, then `POST /v1/jobs/<job id>/replay` |
| A service is not `healthy` | Still starting, or it crashed | Wait 30 s and recheck (§4). Then read its log: `docker compose logs <service> --tail 50`, using a service name from §4. Restart it with `docker compose up -d <service>`; if it still fails, `make down && make up` |
| A gate times out waiting for `DRAFTED` | The ai-worker is down, or the model call failed | `docker compose logs ai-worker --tail 50` |
| Changing `.env` had no effect | Containers read `.env` only when they start | `make up` again |

---

## 8. Reset, switch back, and clean up

- **Reload demo data:** `make seed` (safe to repeat).
- **Stop everything:** `make down`.
- **Go fully offline** (no calls to Google at all, as the automated tests run): in `.env` set `LLM__PROVIDER=fake` and **[after Phase 6]** delete the `GMAIL_ACCESS_TOKEN` line, then `make up`. The other `LLM__*` lines can stay.
- **[after Phase 6] After every demo, revoke Gmail access. This is required, not optional:** the Playground also created a refresh token that stays valid after the access token expires (up to 7 days with your own Testing-status client, Appendix A). Signed in as the test account, open [myaccount.google.com/permissions](https://myaccount.google.com/permissions), select **OAuth 2.0 Playground** (or your own app's name from Appendix A) and remove its access. Then delete `GMAIL_ACCESS_TOKEN` from `.env`.
- **If a secret was ever exposed** (pasted into a chat, shown on a screen, committed):
  - Gemini key: delete it in AI Studio, create a new one, update `.env`.
  - Gmail: revoke access as above and mint a new token next time.
  - Own OAuth client (Appendix A): reset its client secret in the Google Cloud console.

---

## Appendix A: Using your own Google Cloud OAuth client (optional)

Do this **before** §3.2, only if you prefer not to use the Playground's built-in client.

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project and enable the **Gmail API**.
2. Configure the OAuth consent screen as **External**, publishing status **Testing**, and add the test account as a test user.
3. Create an **OAuth client ID** of type **Web application** and add `https://developers.google.com/oauthplayground` as an authorized redirect URI.
4. In the Playground, open the settings (gear icon), tick **Use your own OAuth credentials**, and paste the client ID and secret. Now continue with §3.2 step 1.

Google's OAuth documentation states that a project whose consent screen is External and in Testing gets refresh tokens that expire after 7 days. The stack never uses the refresh token, but it stays usable until it expires or you revoke access (§8).
