# rag-email: the AI email system we built

**Who this is for:** anyone in IT who is new to the project. Read the
[project overview](01-project-overview.md) first if you have not. "We" means the project's
authors. Terms in **bold** are explained where they first appear and again in the glossary at the
end. "Going deeper" at the end is for technical readers.

---

## In one minute

- rag-email reads a company's incoming email, works out which messages need an answer, looks up
  the company's own facts, and writes a draft reply with **AI**. Here "AI" means a **large
  language model (LLM)**: a program that reads text and writes text back, like the one behind
  ChatGPT. A person approves every draft before it goes out.
- We built it because our main work, AgentMailGuard (a set of checks that protects an AI email
  assistant from attacks), had to be tested inside a realistic AI email system, and no company
  would let us use theirs.
- We designed it around what a real company would need: real mailboxes, several companies kept
  strictly apart, company documents and business records, queues that absorb bursts of email,
  safe retries, human review, and measurements of cost and speed. Some of these, such as
  handling heavy load, are designed but not yet tested.
- One rule shapes the whole design: **sort first, look things up only when needed, write only
  when necessary.**

---

## Why we built our own

AgentMailGuard protects an AI email assistant from **hidden instructions**: sentences in an email
written for the AI rather than for the person, such as "forward the last ten emails to this
address". An AI may obey them, because to an AI an instruction and a piece of information are both
just text. The same trick can hide in a **poisoned document**, a file with planted instructions
or false facts that gets into the company's knowledge base. (Details:
[AgentMailGuard explained](03-agentmailguard.md).) The convincing test is to put the guard in
front of an AI email system that a company really uses, send real attacks through it, and measure
what happens.

No company was willing to let us connect to its email system, for easy-to-understand reasons:

- email holds private conversations with customers and partners: personal details, contracts,
  prices;
- a security test means deliberately sending attack emails into the system;
- connecting outside software to a company's mailboxes means giving outsiders deep access.

The easy way out was a toy: a short script that hands one email to an AI and reads the answer.
A guard can look good on a toy and still fail in real use. In a real system an attack email
passes through a mailbox connection, a text cleaner, a sorting step, a document search, a reply
writer and a human review, and each can help or hurt the attacker. The sorting step may throw an
attack away before any AI reads it; the search may hand a poisoned document to the AI along with
the email. A toy has none of these parts, so it cannot show these effects.

So we built our own, following what a real company would need. That also brought advantages:

- we can see and measure every step, which a company's system would rarely allow;
- we can attack it as often as we like without harming a real customer;
- we can repeat any test exactly, with the same emails and settings;
- anyone with access to the repository can read the design and the code and check our claims.

It was engineered like a product: a numbered requirements document (so each test can point to the
requirement it proves) and a design document came first, and the code follows them.

### What "realistic" means here

| A real company needs to… | rag-email… |
|---|---|
| use the mailboxes it already has | connects to Gmail, and to Outlook through Microsoft Graph (Microsoft's official interface to Outlook mail). The Outlook connection has so far been tested only against recorded responses, not a live mailbox |
| serve several client companies from one system without mixing their data | tags every record with its organization. Our test data has three made-up companies (Acme, Beta and Gamma) with overlapping content, so any leak between them would show up |
| answer from its own facts | searches a **knowledge base** (policies, FAQs, manuals) and reads **business records** (customers, products, orders, support tickets) |
| survive a sudden flood of email | puts work in waiting lines (queues, explained below), so extra emails wait their turn |
| never lose an email and never answer twice | makes every step safe to repeat, and hands each approved reply to the mailbox in careful steps so it is never delivered twice |
| keep people in control | keeps every reply as a draft until a person approves it on the review screen |
| keep AI costs predictable | makes one AI call to write each reply, with a small fixed limit on extra calls (sorting a hard email, summarizing a long thread, one format repair), and skips AI writing when it is not needed |
| know how it is doing | records timings, AI use and quality measures (such as how often reviewers edit or reject drafts); ready-made dashboards and full cost reports are planned but not built yet |
| handle a real company's volume | is designed for 10,000 mailboxes and 100,000 emails a day (a design target, not a measurement) |

What is real and what is made up: the Gmail connection is real code, written for a Gmail test
account, but its first live end-to-end check is still to be run. The three companies and their
documents and records are invented. The attack emails are real ones from a public challenge
(Microsoft's LLMail-Inject); the poisoned documents come partly from a public research dataset
and partly from our own templates.

---

## The central rule: sort first, look things up only when needed, write only when necessary

An **AI call** is one request to an LLM: the system sends text and gets text back. It is the
slowest and most expensive step, because an LLM takes seconds and a lot of computing power (a
hosted model is paid by the amount of text it reads and writes). So rag-email makes sure most
emails never need an AI to write for them. The design works with this estimate for a company
receiving 100,000 emails a day (design estimates, not measurements):

```
100,000 emails a day
   │
   ├── ~45,000 need no reply (newsletters, notifications, "thanks!") ──► stop: no AI writing
   │
   ├── ~20,000 ask for a standard answer ("please confirm you got my documents")
   │        ──► filled-in template: no AI writing
   │
   └── ~35,000 need a written reply ──► the AI writes a draft
            │
            ├── ~24,500 need company facts ──► search the knowledge base first
            └── ~10,500 do not ──► no knowledge search
```

The point: the cost of AI writing grows with the number of emails that need a **written** reply,
not with the total number of emails. Searching the company's documents first and giving the
passages found to the AI together with the email is called **RAG** (retrieval-augmented
generation); it gives rag-email its name.

---

## The journey of one email

The official architecture diagram is in `docs/architecture/` (files listed in "Going deeper").
Below is a still picture of it. Its small labels are technical shorthand; the table after it
explains each of the twelve parts in plain words.

![The rag-email architecture diagram. Top row: Mail Connector Service, RabbitMQ Ingest, Email
Processing Service, Cascading Triage Engine. Middle row: Email Providers, Knowledge Ingestion,
PostgreSQL Platform, Hybrid RAG Engine, RabbitMQ Categories. Bottom row: Draft Store &
Dispatcher, Reply Agent & Model Cascade, Context Builder. Four cards below summarize the
principles, the search, the reliability rules and the
deployment.](../architecture/enterprise-rag-email.architecture.visual-check.2048x1320.light.png)

| Part (its name on the diagram) | What it does | Step |
|---|---|---|
| Email Providers | The mail services the company already uses: Gmail, and Outlook through Microsoft Graph. The design also allows IMAP, the older generic mail protocol; the current code connects to Gmail and Outlook only. | — |
| Mail Connector Service | Notices new mail, fetches it, and remembers where it stopped. | ① |
| RabbitMQ Ingest | The first queue (RabbitMQ is the queue software): new emails wait here until a worker is free. | ①→② |
| Email Processing Service | Unpacks and cleans the email, and links it to its conversation. | ② |
| Cascading Triage Engine | Sorts the email in levels, cheapest first: rules, a classifier, then a small AI model. | ③ |
| RabbitMQ Categories | Two queues per category, a normal one and an urgent one, so urgent emails go first. | ③→④ |
| Context Builder | Gathers what the AI will read: the conversation, the business records and, if needed, company knowledge. | ④ |
| Hybrid RAG Engine | Searches the company's knowledge by words and by meaning. | ⑤ |
| Knowledge Ingestion | Prepares the company's documents for searching, ahead of time. | before any email |
| PostgreSQL Platform | The one database: emails, conversations, business records and the search indexes (lookup structures that make search fast). | most steps |
| Reply Agent & Model Cascade | Picks the AI model and has it write the draft in a fixed format. | ⑥ ⑦ |
| Draft Store & Dispatcher | Keeps the drafts and, after approval, puts each reply into the mailbox exactly once: as a draft for a person to send (the default) or sent directly. | ⑧ |

The same journey in a simpler picture:

```
 Gmail / Outlook
      │
      ▼
 ① Notice and fetch ───── save the original email, remember where we stopped
      │  (queue)
      ▼
 ② Clean ──────────────── readable text, old quoted replies split off, duplicates dropped
      │  (queue)
      ▼
 ③ Sort (triage) ──────┬─ no reply needed ─────────────► done, no AI writing
      │                └─ standard answer ─────────────► template draft ──► ⑧
      │  (a normal and an urgent queue per category: billing, support, sales, …)
      ▼
 ④ Gather context ─────── conversation so far + business records
      │        │
      │        └──► ⑤ Search company knowledge (only if ③ said it is needed)
      ▼
 ⑥ Write the draft ────── one AI call, fixed answer format, sources listed
      │
      ▼
 ⑦ Check the format ───── one repair attempt; a broken answer is never saved
      │
      ▼
 ⑧ Review and hand over ─ a person approves; placed once in the same conversation
                          (as a draft in Gmail or Outlook by default)
```

The template shortcut comes from the design document and the code; the official diagram does not
draw it.

### ① Notice and fetch (Mail Connector Service)

In a real deployment, Gmail and Outlook tell the system when a mailbox changes (a **push
notification**), so it never has to keep asking "anything new?". It then fetches only what
changed and records where it stopped (a **checkpoint**). The checkpoint moves forward only after
the new emails are safely stored, so after a crash the system re-reads a few emails, which is
harmless, and never skips any. The original email file is kept exactly as it arrived, in
**object storage** (file storage for large items). rag-email runs today on a developer's laptop,
which Gmail's notifications cannot reach, so there a person starts the fetch with one request to
rag-email's own interface.

### ② Clean (Email Processing Service)

An email travels as a raw bundle: headers, plain text, an HTML version and attachments, packed
together in the **MIME** format. This step unpacks it into readable text, separates the new
message from quoted older replies and the signature, recognizes an email delivered twice, and
links the email to its conversation (its **thread**).

### ③ Sort (Cascading Triage Engine)

**Triage** decides what the email is. It uses three levels, cheapest first ("cascading": each
level passes what it cannot decide down to the next). Each level gives a **confidence score**
from 0 to 1 and decides only when the score is above its threshold:

1. **Fixed rules** catch the obvious cases instantly: automatic notifications, newsletters,
   out-of-office replies, bounced emails.
2. A **classifier** built with **machine learning** (it learned patterns from many labeled
   example emails instead of being given rules) is meant to decide most of the rest, in a few
   hundredths of a second. It is not an LLM and writes no text.
3. **A small AI model** (a small LLM) is asked only about emails the first two levels cannot
   decide. If it is unsure too, the email gets a safe default: a general question that needs a
   written reply, flagged for review.

The result is the email's category (support, sales, billing, scheduling and a few others),
whether it needs a reply, whether it needs company knowledge, and how urgent it is. The rules and
the small AI model can also say what the sender wants; the classifier gives only the category.
An email that needs no reply stops here: no search and no AI writing. If the small AI model had
to sort it, that one small call is its only AI cost. An email with a standard answer gets a
**template**, a pre-approved reply with blanks filled in, with no search and no AI writing. (In
today's code only the small AI model picks the template route.)

### Queues between the steps (RabbitMQ Ingest and RabbitMQ Categories)

Each step hands work to the next through a **queue**, a waiting line of jobs. The diagram draws
the two main ones; the others, such as the queue in front of sorting and the one for handing
replies over, live in the same RabbitMQ. If a thousand emails arrive in one minute, they wait
instead of overloading the system, and extra **workers** (copies of the program that takes jobs
from a queue) can be started to clear the line faster. Urgent emails go first. One worker may
handle several emails at a time, but each email gets its own AI call. Emails are never mixed into
one AI input, because one email's content could then change or leak into another's answer.

### ④ Gather context (Context Builder)

The AI needs the right information, and only that. The **context** for one email has three
parts:

- **The conversation so far.** For a longer thread (by default, from four messages), the system
  keeps a short AI-written summary of the older messages plus the latest ones word for word.
  The summary is one of the allowed extra AI calls, and it is redone only when the thread has
  grown enough, not on every new message.
- **Business records.** If the email mentions an order or a customer, the system reads those
  records, for example the order's status, total and dates.
- **Company knowledge**, but only if triage said it is needed (step ⑤).

### ⑤ Search company knowledge (Hybrid RAG Engine)

The knowledge base holds the company's procedures, FAQs, manuals and reply templates. When a
document is added, Knowledge Ingestion splits it into short passages (**chunks**) and prepares
them for searching, ahead of time. When an email needs knowledge, the system builds a search
query from the email (plus the thread summary, if any) without any AI call, and runs a **hybrid
search**, two ways at once:

- **By words**: passages containing the exact words. Good for order numbers, product names and
  error codes.
- **By meaning**: a separate model turns every passage, and the query, into a long list of
  numbers, an **embedding**. It was trained so that texts with similar meanings get similar
  lists, the way nearby places have similar map coordinates. Passages close to the query are
  found even when they use different words, such as "money back" and "refund".

Each search returns its top 20 passages. **Reciprocal rank fusion** merges the two lists: a
passage scores 1 / (60 + its position) in each list and the scores are added, so a passage ranked
high in either list rises. Then a **re-ranking** model, slower but more careful, reads the query
with each candidate and scores how well it answers. It only scores; it writes no text and is not
an LLM call. If it is unavailable or too slow, the merged order is kept. Only the best four to six
passages go to the AI: extra passages distract it and cost more.

### ⑥ Write the draft (Reply Agent & Model Cascade)

A routing rule picks the AI model: a cheaper routine model for most emails, a more capable and
expensive one for harder ones. An email counts as harder when, for example, triage was unsure,
the thread is long, the search found little good evidence, or the email asks for several things.
This is the **model cascade** (here "cascade" means stepping up to a stronger model only when
needed). Whichever model is picked, it is called **once** to write the draft; the only extra call
allowed is one repair in step ⑦. It receives this category's instructions (an **agent profile**
that sets the tone and what the reply may promise), the cleaned email, the conversation, the
business records and the passages found, each clearly labeled. It must answer in a fixed format:
the action (reply; forward to someone else; hand over to a person; or no reply), the reply text,
and the passages it relied on (its **citations**).

### ⑦ Check the format (Reply Agent & Model Cascade)

If the AI's answer does not match the required format, the system asks it once to repair it. If
it still fails, the job goes straight to the dead-letter queue (see "What keeps it reliable") for
a person to inspect; a broken or unchecked answer is never saved. Citations are checked too. An
LLM can state things that are not true, and citing a passage it was never shown is a sign of
such invented content, so the draft is flagged and the case is counted as a quality measure.

### ⑧ Review and hand over (Draft Store & Dispatcher)

The draft appears on the review screen. A person reads it, edits it if needed, and approves or
rejects it; a rejected draft is never sent. After approval, rag-email puts the reply into the
same conversation in the company's mailbox. By default it lands there as a draft, and a person
presses send in Gmail or Outlook; a category can instead be set so that rag-email sends it after
approval. The hand-over runs in careful steps: claim the job, create the draft in Gmail or
Outlook, send it (only in the sending setting), and record the result. If a crash interrupts
this, the retry first asks Gmail or Outlook whether that draft was already sent, and sends only
if it was not. So a crash halfway through does not make the customer get the same reply twice.

---

## Worked example: "I was charged twice"

The names, order number and policy wording below are invented for illustration.

Acme's billing mailbox receives this email from a customer, Lan:

> **Subject:** Charged twice for order 10482
>
> Hello, I was charged twice for order 10482 last week. Can I get the extra payment back?
> Thanks, Lan

① **Notice and fetch.** Gmail tells rag-email the mailbox changed (on a developer's laptop, the
manual fetch request does this). The connector fetches the email, stores the original file,
moves its checkpoint, and queues a job.

② **Clean.** The text is extracted. There is no quoted history, so this starts a new thread.

③ **Sort.** No fixed rule applies. The classifier is confident that this is a *billing* email,
and for that category the system knows a reply and company knowledge (the billing policy) are
needed. No LLM was used to sort it. The job enters the normal billing queue.

④ **Gather context.** The thread has one message, so there is nothing to summarize. The email
names order 10482, so the system reads that order's record: its status, total and order date.
The system holds no payment records, so it cannot see the double charge itself.

⑤ **Search.** The search by words finds the "Duplicate charges" section of the billing policy,
because it contains "charged twice". The search by meaning finds "Refunds go back to the original
payment method within 5–7 business days", though the email never says "refund". Fusion and
re-ranking put both at the top of the few passages given to the AI.

⑥ **Write the draft.** One call to the routine model produces: an apology, the order's details,
a promise that the team will check the duplicate charge, a statement that a duplicate charge is
returned to the original card within 5–7 business days, citations to the two policy passages,
and the action *reply*.

⑦ **Check the format.** The format is right, and both citations point to passages the AI was
shown.

⑧ **Review and hand over.** A member of the billing team sees the draft, checks the order, and
approves it. rag-email then puts the reply into the same Gmail conversation as a draft, exactly
once, and the team member sends it from Gmail.

For contrast, three other emails that arrive the same morning:

| Email | Where it stops | AI used? |
|---|---|---|
| A supplier's newsletter | ③ Sort: a fixed rule sees the newsletter headers, no reply needed | No |
| "Thanks, got it!" | ③ Sort: the classifier decides no reply is needed | No |
| "Please confirm you received my documents" | ③ Sort: the small AI model recognizes a standard request, so a template reply goes to review | Small AI to sort; no AI writing, no search |

---

## What keeps it reliable

- **Any job may arrive twice.** A worker tells the queue a job is done only after its results
  are safely stored. If it crashes after the work but before saying so, the queue hands the job
  out again. So the queue promises delivery *at least* once, not *exactly* once, and each step
  first checks "have I already done this?". A step that is safe to repeat is **idempotent**.
- **Every email has one clear status.** Each job moves through fixed stages, and only allowed
  moves are possible. This is a **state machine**. It shows where any email is and makes it easy
  to resume after a crash. The main path, as the diagram names it:

  ```
  RECEIVED → NORMALIZED → CLASSIFIED → QUEUED → CONTEXT_READY → GENERATING → DRAFTED → DISPATCHED → COMPLETED
  received   cleaned      sorted       in line  context ready   AI writing   draft     handed over  done
  ```

  Shortcuts: a no-reply email goes from CLASSIFIED straight to COMPLETED (the diagram's "No
  Reply → COMPLETE"); a template reply goes from CLASSIFIED to DRAFTED; a rejected draft goes
  from DRAFTED to COMPLETED with nothing sent.
- **Failing jobs are retried, then parked, not lost.** If the AI call fails, for example because
  the model service is busy, the job goes to `RETRY_PENDING` and tries again after a pause that
  grows each time. When its retries run out, or when a draft is still broken after its one
  repair, it moves to `DEAD_LETTER`, in a separate **dead-letter queue** for a person to inspect.

---

## What rag-email does not do

- **No security checks of its own.** Detecting hidden instructions, spam, phishing or malware,
  and handling logins and access rights, were kept out on purpose; they belong in separate parts.
  AgentMailGuard is the part for hidden instructions; [AgentMailGuard explained](03-agentmailguard.md)
  shows where its layers attach to rag-email's twelve parts.
- **No login on the review screen.** Anyone at the screen can see and approve drafts, so it runs
  only on a developer's own computer.
- **No automatic sending.** Every category is set to "create a draft": after approval the reply
  waits in the mailbox's Drafts folder, and a person sends it from there. No category may send
  without approval.
- **Not a chatbot.** It holds no live conversations: each incoming email is handled once, on its
  own, and produces at most one draft.

---

## Going deeper (for technical readers)

| Step | Process (service) | Main technology |
|---|---|---|
| ① Notice and fetch | `mail-connector` | Gmail API, Microsoft Graph, change notifications, checkpoints; original email files in MinIO (S3-compatible object storage) |
| ② Clean | `email-worker` | MIME parsing, HTML cleaning; attachments stored in MinIO |
| ③ Sort | `triage-worker` | rules, then TF-IDF + logistic regression (a classic word-count classifier), then a small LLM |
| Queues | RabbitMQ | retry ladder (retries at growing intervals), dead-letter queues, a normal and a priority queue per category |
| ④–⑦ Context, search, draft | `ai-worker` | PostgreSQL full-text search + `pgvector` (HNSW index for fast nearest-neighbor search), reciprocal rank fusion, cross-encoder re-ranker, a JSON reply schema (`reply.v1`) |
| Adding documents | `knowledge-worker` | chunking (about 350–700 tokens per chunk; a token is a word piece, roughly ¾ of a word), embeddings |
| ⑧ Review and hand over | `api`, `frontend`, `dispatch-worker` | FastAPI, Jinja2 and htmx for the review screen |
| Measurements | Prometheus, Grafana | structured logs and metrics today; traces and Grafana dashboards are partly built |

- **Diagrams** in `docs/architecture/`: `enterprise-rag-email.architecture.html` (open in a
  browser; five guided views: the end-to-end email path, selective hybrid RAG, the knowledge
  ingestion pipeline, async reliability and recovery, and scale, deployment and operations),
  `enterprise-rag-email.architecture.drawio` (editable in draw.io; it uses two shorter names,
  "Email Processor" and "Reply Agent & Cascade") and `enterprise-rag-email.architecture.json`
  (the source the diagram is drawn from, whose names this document uses).
- **Deployment:** for this project, everything runs with Docker Compose (`make up`): FastAPI,
  PostgreSQL with `pgvector`, RabbitMQ, MinIO, Prometheus and Grafana. For a large company, the
  design scales out to pools of stateless workers, a RabbitMQ cluster, a managed database and S3
  storage, adding workers as queues grow.
- One PostgreSQL database holds both everyday data and search indexes; a separate search engine
  would be added only if measurements showed PostgreSQL was not enough.
- All AI calls go through one interface (`LLMProvider`) and all searches through another
  (`SearchBackend`), so a model or search method can be swapped without touching the rest.
- Mail providers are reached only through `MailProviderAdapter`; all Gmail- and Graph-specific
  code lives in `packages/adapters/`.
- Read more: the proposal in `docs/proposal/`; the numbered requirements (`R1.1` to `R24.7`) in
  `specs/requirements.md`; the design in `specs/design.md`; the work plan in `specs/tasks.md`;
  the project rules in `CLAUDE.md`.

---

## Glossary

| Term | Meaning |
|---|---|
| AI, LLM | Here, a large language model: a program that reads text and writes text back. A "small AI model" is a smaller, cheaper LLM. |
| AI call | One request to an LLM. Slow and costly compared with ordinary software. |
| Hidden instructions | Sentences in an email written to steer the AI rather than to inform the reader. |
| Poisoned document | A knowledge-base document with planted instructions or false facts. |
| RAG | Retrieval-augmented generation: search the company's documents first, then give the passages found to the AI with the email. |
| Push notification | A message from Gmail or Outlook saying "this mailbox changed". |
| Checkpoint | A saved marker of how far the system has read a mailbox. |
| Object storage | File storage for large items such as original emails and attachments (MinIO here). |
| MIME | The standard format of an email in transit: headers, text, HTML and attachments packed together. |
| Thread | One conversation: an email and all the replies to it. |
| Triage | Sorting an email: what it is, whether it needs a reply or company facts, how urgent it is. |
| Confidence score | A number from 0 to 1 for how sure a sorting level is; below its threshold, the next level decides. |
| Classifier, machine learning | A classifier sorts inputs into categories; with machine learning it learned how from labeled examples, not written rules. |
| Template | A pre-approved reply with blanks filled in, with no AI writing. |
| Queue, worker | A queue holds jobs waiting to be done; a worker is a program copy that takes jobs from it. |
| Context | Everything given to the AI for one email: the email, the conversation, records and passages. |
| Knowledge base | The company's documents that the system searches. |
| Business records | The company's structured data: customers, products, orders, support tickets. |
| Chunk | A short passage of a document, the unit the search returns. |
| Hybrid search | Searching by exact words and by meaning at the same time. |
| Embedding | A list of numbers representing what a text means; similar meanings get similar numbers. |
| Reciprocal rank fusion | Merging two ranked lists by adding up a score for each item's position in each list. |
| Re-ranking | A slower, more careful pass that scores each candidate passage against the query. |
| Model cascade | A cheaper AI model for routine emails, a more capable one only for hard cases. |
| Agent profile | The instructions for one category: tone, what the reply may promise, what it must not do. |
| Citation | A pointer from the draft to the passage it relied on. |
| Idempotent | Safe to do twice: doing it again changes nothing. |
| State machine | A fixed set of stages and the allowed moves between them. |
| Dead-letter queue | Where jobs that keep failing are parked for a person to inspect. |

---

## Check yourself

1. Give two reasons why a company would not let us test on its real email system.
2. Why would a guard that looks good on a "toy" email script prove little?
3. An email says only "Thanks, got it!". Which steps does it pass through, and is the AI used?
4. Why does the search run both by words and by meaning?
5. Why does the checkpoint move forward only after the emails are saved?
6. What happens when the AI's answer does not match the required format?

<details>
<summary>Answers</summary>

1. Email holds private customer conversations, and a security test means deliberately sending
   attacks into the system. It would also give outsiders deep access to the company's mailboxes.
2. Real attacks pass through many parts (fetching, cleaning, sorting, search, review) that can
   stop them or help them. A toy has none of these parts, so it cannot show these effects.
3. Notice and fetch, clean, then sort. Triage decides that no reply is needed and processing
   ends there: no search and no AI writing. Normally the rules or the classifier decide this, so
   no AI is used at all; only if both were unsure would the small AI model sort it.
4. Searching by words catches exact terms such as order numbers and product names. Searching by
   meaning catches passages that say the same thing in different words. Each covers the other's
   blind spot.
5. If the system crashed after moving the checkpoint but before saving, those emails would be
   skipped for good. The other way round, it only re-reads a few emails, which is harmless.
6. The system asks the AI once to repair its answer. If it is still wrong, the job goes straight
   to the dead-letter queue for a person to inspect, and the broken answer is never saved.

</details>
