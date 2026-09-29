# Phase 5 business data: when to fetch, and how the three models fit

Date: 2026-09-28. Branch: `RAG_Email_System`. Scope: Q1 and Q2 of the Phase 5 research brief. Q3 belongs to the controller and is not covered here.

Status: research note only. It changes no code and no spec. Anything below that would change `specs/design.md`, the taxonomy or the config model needs the owner's decision first (CLAUDE.md:111-123, divergence protocol).

How to read it: each section separates **what sources say** (numbered references), **what the repo does** (file:line), and **the recommendation**.

## Short answer

- **Q1.** Decide the fetch in code, before the single generation call. Use signals the pipeline already has: the sender address, typed order and ticket IDs in the email, the category (through the profile's existing but unused `context_policy`), and the intent when there is one. Do not let the model call a lookup tool. That needs a second model request and breaks the one-generation budget.
- Do not key the fetch on intent alone. The ML stage always sends `intent=None`, so a new `order_status` intent would never reach the Context Builder for ML-decided emails.
- Every lookup returns an explicit status (FOUND, NOT_FOUND, NOT_LOOKED_UP, UNAVAILABLE, ...). Business facts go in one labelled `[BUSINESS DATA]` block. The prompt says statuses, dates and amounts come only from that block.
- **Q2.** Serve Qwen and Llama through Ollama at one base URL. Because facts are pre-fetched, none of the three models has to call a tool. The ID lookups are then the same for every model. The category- and intent-driven snapshot is not: it follows triage, and Stage 3 triage runs on the model under test. So hold triage on one provider when comparing drafts. Suggested slots: GPT-4.1 as the API baseline, Qwen3-8B, and Llama-3.1-8B-Instruct as its size match (§3.3). The repo today cannot select Qwen (local model names are hard-coded to Llama), and it cannot mix GPT and a local model across tiers (one provider for everything).
- The Q1 recommendation keys the fetch on IDs and category, not only on "the entities the intent names". That is a spec divergence: owner decision, `specs/design.md` §5.4 update, an ADR (which must also say how typed-ID lookups relate to `context_policy`), and a `specs/tasks.md` 5.4 sync.

## 1. What the repo does today

Repo facts come from the repo mapper, plus two read-only checks of `docker-compose.yml` and `.env.example` made for this report.

**The trigger has nothing to key on.**

- No intent names order or ticket status. Billing has `invoice_inquiry` and `receipt_lookup` (packages/domain/taxonomy.py:103; config/categories.yaml:50). Task 5.4 assumes such intents exist (specs/tasks.md:621).
- The ML stage always emits `intent=None` (services/triage_worker/classifier.py:187). The ML threshold is 0.80 (packages/core/settings.py:295). Every email the ML stage decides reaches the Context Builder with no intent.
- The LLM stage's intent is a free string. Its prompt never lists allowed intents (services/triage_worker/llm_classifier.py:38, :70). Nothing checks any intent against the taxonomy (packages/domain/taxonomy.py:55).
- Rules copy a fixed intent through (packages/domain/rules.py:413). Only `invoice-reference` matches an ID (config/triage_rules.yaml:108). No rule matches an order or ticket ID.
- The only ID extractor is the retrieval query builder. It drops the ID type and is noisy: "in order to fix" gives `ORDER TO`, and "Where is my order #82915?" gives nothing (packages/retrieval/query_builder.py:247, :262). It runs only in the RAG branch, and only when `retrieval_required` is true (packages/context/builder.py:172).

**The business path is a stub.**

- `get_business_data(organization_id, message, intent)` returns an untyped dict. It receives no category, no IDs and no timeout (packages/context/builder.py:44). The stub returns `{}` (:66). It is called on every job with no timeout, no try/except and no degraded flag (:201). The CONTEXT_READY payload has no business fields (:225). `packages/business/` is empty.
- The production ai-worker does not pass a business provider, so it uses the stub (services/ai_worker/main.py:99).
- A per-profile switch for business data already exists but does nothing. `AgentProfile.context_policy` has a `thread_plus_rag_plus_business` value (packages/llm/profile.py:36, :63). Only the billing profile sets it (config/agent_profiles.yaml:29; the design shows the same, specs/design.md:553). The Context Builder never reads it and calls the business provider on every job (packages/context/builder.py:201). The 2026-09-26 audit already logged this (artifacts/superpowers/2026-09-26-audit-register.md:1128).
- The Jinja templates render business data under four different headers, none of them `[BUSINESS DATA]` (prompts/billing.v1.j2:38). `get_ordered_sections` is used only for token counting (packages/llm/router.py:402). The reply schema has no field for business facts used (schemas/reply.v1.json:26).
- Useful precedents: anything placed in `classification.raw` reaches the ai-worker (packages/broker/routing.py:177; services/ai_worker/consumer.py:45). The retriever already records `retrieval_degraded` (packages/retrieval/retriever.py:52).

**The Phase 5 gate data does not line up yet.**

- Order 82915 (task 5.6) is not seeded (specs/tasks.md:631). The seeded orders are ORD-9901 (Dana, processing) and ORD-8820 (Alice, shipped) (packages/db/fixtures/business.py:94).
- Fixture email `identifier_order_ticket` comes from Edward but asks about ORD-9901, which is Dana's (packages/db/fixtures/emails.py:208). Under R13.4 scoping, that order must come back NOT_FOUND. Dana's own `long_thread_dispute` email asks about her ORD-9901 (:164).
- The knowledge chunk "Order Status & Tracking Numbers" names ORD-9901 and ORD-8820 (packages/db/fixtures/knowledge.py:186). It is the procedure chunk the gate needs to cite. It is also the RAG text that R13.3 says must not supply the status.
- There is no invoice table, yet fixture `billing_invoice` asks about INV-2026-8891 (packages/db/fixtures/emails.py:99).

## 2. Q1: when to fetch transactional facts

### 2.1 What the sources say

**Products use two trigger patterns.**

- *Intent or use-case gated.* A classification picks a procedure, and the procedure calls the integration.
  - Zendesk ties each generative procedure to a use case. A procedure can include a "Run integration" block that looks up an order [6]. Its order-status example skips asking for the number if it is already known, then calls `/checkOrderStatus` [7].
  - Gorgias classifies each conversation into built-in intents and runs the matching skill [12]. Order status maps to its `Shipping/Status` intent [13]. Actions run only from inside a skill or guidance [12].
  - Salesforce's router classifies intent. It sends unverified users through identity checks before Order Management [10].
  - Intercom Procedures match the message against a "When to use this procedure" description [2].
- *Model-chosen.* The model decides from a tool description.
  - Intercom's "direct trigger" data connectors work this way. Intercom calls them "best for read-only connectors like 'Check order status'" [1].
  - Ada's API tools can work this way, but only after an admin clears a default box that limits them to Processes or Playbooks [14].
- *Code-driven pre-fetch* also appears. Salesforce's example runs `lookup_current_order` in the reasoning block when `order_summary` is empty [11]. Freshdesk can run a workflow such as "fetching recent orders" when a chat starts [16].

**Email specifically.** Zendesk's email AI agents (generally available 2026-04-20, Advanced plan) run business procedures through integrations and collect the needed information in a single email [9]. Freshdesk says API actions in email workflows "execute as part of the AI agent's response generation cycle", and confirmation steps are skipped [15].

**Model-chosen tool calls cost a second model request.** OpenAI's flow: send tools, receive a call, run it in the application, then make a second request with the tool output [25]. Anthropic: for client-executed tools, every tool call is a round trip, in which the model asks, the application runs the tool and reports back, and the model continues [21]. Meta's Llama 3.1 format has the model emit a call, and the result comes back in a separate `ipython` message [47].

**Provider guidance favours code paths where they are enough.**

- Anthropic separates workflows (predefined code paths) from agents. It recommends the simplest solution, and says a single LLM call with retrieval is often enough [19]. Its routing pattern sends classified inputs to specialised tools, with the classifier being an LLM or a traditional model [19]. It also describes a hybrid: retrieve some data up front, explore more at runtime [20].
- OpenAI says a deterministic solution "may suffice" when a use case does not clearly need an agent [24]. Its function-calling guide advises offloading work from the model to code, and not making the model fill arguments you already know; pass them in code instead [25].
- Anthropic suggests one combined `get_customer_context` tool over many small ones, returning only high-signal fields [22].
- Counterpoint: Anthropic also calls customer support "a natural fit" for open-ended agents with tools for order history [19].

**Research points the same way.**

- Query types line up with retrieval paths well enough for rules to route them. Fact-centric questions usually do better with database lookups, and some questions that one path answers correctly can fail when both sources are added [30].
- Preloading a user's data into the prompt removes the need for the model to query the database. The cost is tokens. The paper proposes it as a defence against prompt-to-SQL attacks [34]. Free-form text-to-SQL solved 21.3% of enterprise-scale tasks on Spider 2.0 [35]. Tool-using agents such as gpt-4o succeeded on under 50% of τ-bench tasks [36].
- LLMs are weaker than task-specific models at tracking slot values, but do well when given correct slot values [32]. Order delivery status helped a Walmart model work out ambiguous queries [42]. A 2026 preprint argues users often put several intents in one message [33].
- Retrieved text can mislead. Legal RAG tools still hallucinated 17–33% of the time, less than GPT-4 alone [37]. Models adopted incorrect retrieved content over 60% of the time, overriding their own correct answer [38]. Larger models often answer wrongly instead of abstaining when the context is insufficient [39].

**Failure handling.**

- Intercom: when a connector fails, times out or returns an error, Fin does not fall back on its own. Unless a guidance rule tells it to decline or hand off, the model can generate fabricated account-specific details [3]. Of the two fixes Intercom gives, the one it calls "the most reliable" is to return an empty or default result instead of an error, for example instead of a 404 for a missing order [4]. Fin may also ignore a successful connector result if other content seems more relevant [1].
- Zendesk integrations end in a non-editable Fallback scenario [8]. Freshdesk routes a timeout or failure to a Fallback path in chat agents [15].
- AWS: set an explicit request timeout and do not trust defaults [58]. Serving no data when partial results are possible is an anti-pattern, and the failure path should be simpler than the main path and tested [57].

**Identity.** None of the sources describes trusting an email From address as a verified identity. Intercom can verify email or user ID only when it comes from Messenger, and suggests JWT or a one-time passcode [5]. Shopify asks the customer to sign in before sharing order information [17]. Freshdesk cross-checks the order ID plus email or phone against the backend [16]. Salesforce Email-to-Case leaves the contact blank when two contacts share an address [18].

### 2.2 The options

All three keep the budget: no new model call, and facts are fetched before the one generation call. They differ in what decides the fetch. All of them skip the fetch when no generation will run (`reply_required=false` or `workflow_hint=template`, CLAUDE.md:80).

Putting business facts into template replies is out of scope for Phase 5. The template registry is keyed by (category, intent), and its render step accepts `business_data` for variable substitution (packages/domain/templates.py:248, :297; services/triage_worker/gate.py:359). But the triage consumer calls the gate without it (services/triage_worker/consumer.py:175). "Template" means zero retrieval and zero generation, not zero lookups. So a later phase could call the same pure fetch-plan module from the triage worker before rendering. In Phase 5, templates keep rendering without business facts.

**Option 1: new intents plus an intent → fetch table (closest to the spec text).**
Add `order_status` and `ticket_status` to the taxonomy, `config/categories.yaml`, the LLM prompt, and a rule for order/ticket ID patterns. The Context Builder looks up a fixed table: intent → entities to fetch. This is the Gorgias and Zendesk pattern [12][13][6], and it matches R13.3 and design §5.4 wording.

- Pro: closest to the spec. Easy to explain in the thesis.
- Con: it misses most traffic. ML-decided emails carry `intent=None`, so the table never fires for them. Fixing that means retraining ML with intent labels, and the dataset's intents use a different vocabulary (evaluation/datasets/generate_classification.py:194). One intent per email also misses mixed requests [33]. It touches the taxonomy, YAML, rules, prompt and eval data.

**Option 2: a code-side fetch plan from sender, IDs, category and intent (recommended).**
A pure function builds a fetch plan before the provider is called:

1. Resolve sender → customer whenever step 2 or 3 plans a lookup.
2. For each typed ORD- or TICK- ID in subject and body, plan a lookup scoped to that customer. Record INV- IDs as NOT_LOOKED_UP with the reason `unsupported_entity` until invoice scope is decided (§4). Never use NOT_FOUND for them: there is no invoice table, and NOT_FOUND would tell the customer the invoice does not exist.
3. If the routed profile's `context_policy` is `thread_plus_rag_plus_business`, or the intent maps to an entity (`invoice_inquiry`, `receipt_lookup`, later `order_status`), plan a small snapshot: the last few orders and open tickets.
4. Everything else is recorded as NOT_LOOKED_UP.

The intent is one input, not the only one. `context_policy` is the repo's existing per-profile gate for business data (§1), so step 3 uses it: which categories get a snapshot is set in `config/agent_profiles.yaml`, not in new code. Step 2 deliberately bypasses it, so a typed order ID in a `general_inquiry` email is still looked up. That override supersedes `context_policy` for typed IDs, and the same ADR must record it (§2.3).

- Pro: works for rule-, ML- and LLM-decided emails alike. The ID-driven lookups involve no model, so they are identical on GPT, Qwen and Llama. The snapshot is only as stable as triage. When Stage 3 decides (ML confidence below 0.80), the category and intent come from the triage model. In a whole-stack run that is the model under test, because triage and generation share `settings.llm` (packages/core/settings.py:216; services/triage_worker/main.py:126; services/ai_worker/main.py:83). Given the triage output, the plan is deterministic and unit-testable with the fake provider. It matches the pre-fetch pattern [11][16][34] and OpenAI's advice to offload work to code [25].
- Con: a question with no ID ("where is my stuff?") only gets the snapshot, and only if its profile's `context_policy` allows it. No category names orders, so such an email may land in `general_inquiry`, whose profile does not allow it today (config/agent_profiles.yaml:61). Turning it on there, or adding an orders category, is a tuning choice for the owner (my inference). The ID regex must be rewritten to keep the type and require a digit. It diverges from "fetch only the entities the intent names" (specs/design.md:402).

**Option 3: add `data_needs` and `entity_refs` to the triage output.**
Extend the Stage 3 triage schema with a multi-label enum (`order`, `ticket`, `invoice`, `customer_profile`) and a list of typed references. Carry both in `classification.raw`, which already reaches the ai-worker. Rules fill `entity_refs` first by regex. This separates "what the customer wants" (intent) from "what data the reply needs".

- Pro: handles mixed emails and ID-less questions better. Still one triage call. On OpenAI, Structured Outputs keeps the model from inventing an enum value [26]. For the two local models this is unverified. Ollama documents schema enforcement through `format` or `response_format` [48], but whether Ollama or llama.cpp honour `strict: true` as the client sends it waits on the §3.3 item 5 smoke check. A cheap classifier with LLM fallback came within 2% of LLM accuracy at half the latency in one study [31].
- Con: only emails that reach Stage 3 (ML confidence below 0.80) get `data_needs`. ML-decided emails still need Option 2's code path. It changes the triage schema, which already has an open strict-mode question (§3.2).

**Not an option under the current spec: tool calling during generation.** The model would request `get_order(...)`, the worker would run it, and a second model request would write the reply [25][21]. That breaks "exactly one generation call" (CLAUDE.md:79; R14.9) and would need a spec change. It also leans on a weak point of small models. On BFCL V4, "irrelevance detection" (not calling a tool when none fits) is 42.70% for Llama-3.1-8B (prompt mode), 79.07% for Qwen3-8B and 86.52% for GPT-4.1 [44]. Ollama's OpenAI-compatible endpoint does not support `tool_choice` [49], so a call cannot be forced there.

| | Option 1: new intents | Option 2: code fetch plan | Option 3: triage `data_needs` |
|---|---|---|---|
| Works when ML decides (intent=None) | No | Yes | No, needs Option 2 too |
| Extra model calls | 0 | 0 | 0 (same triage call) |
| Same fetch on GPT, Qwen, Llama | Depends on triage model | ID lookups: yes. Snapshot: depends on triage model when Stage 3 decides | Depends on triage model |
| Several requests in one email | Weak | Yes, by IDs | Yes |
| "Where is my order?" with no ID | Only if intent set | Snapshot, if category matches | Yes, if Stage 3 ran |
| Spec work | Taxonomy, prompts, eval data | design §5.4, ADR | design, triage schema, ADR |
| Effort | Medium | Low to medium | Medium |

### 2.3 Recommendation

Build **Option 2** now. Feed the intent into the plan as one extra signal. Keep Option 3 as a later upgrade if the evaluation shows ID-less questions being missed. Adding `order_status` and `ticket_status` intents (Option 1) is cheap and can feed the same plan, but do not rely on it alone.

```
inbound email
    │
    ▼
triage cascade: rules ──▶ ML ──▶ LLM              (≤1 triage call, unchanged)
    │  category, intent (None when ML decides), retrieval_required
    │  classification.raw may carry rule-found IDs (optional)
    ▼
per-category queue ──▶ ai-worker
                          │
                          ▼
               Context Builder.build_context
   ┌──────────────────────┼─────────────────────────────────┐
   │                      │                                 │
thread state         RAG branch                  FetchPlan (pure code, no model)
(≤1 summarize)       (if retrieval_required)     inputs: sender, category,
   │                 chunks + chunk ids          intent, typed IDs, profile
   │                      │                      context_policy
   │                      │                      1. sender → customer (when
   │                      │                         2 or 3 plans a lookup)
   │                      │                      2. each ORD-/TICK- id →
   │                      │                         lookup scoped to customer
   │                      │                         (INV- → NOT_LOOKED_UP)
   │                      │                      3. context_policy allows
   │                      │                         business, or intent maps
   │                      │                         → small snapshot
   │                      │                                 │
   │                      │                                 ▼
   │                      │                  BusinessDataProvider under a timeout
   │                      │                  (Postgres now, CRM/ERP adapter later)
   │                      │                                 │
   │                      │       FOUND · NOT_FOUND · NOT_LOOKED_UP · UNAVAILABLE
   │                      │       UNKNOWN_SENDER · AMBIGUOUS_CUSTOMER
   ▼                      ▼                                 ▼
ContextPackage, fixed order: instructions · summary · recent msgs · email ·
   knowledge chunks · [BUSINESS DATA] (source, as-of, one fact per line)
CONTEXT_READY payload: fetch plan + business_data_degraded
    │
    ▼
exactly ONE generation call   (GPT │ Qwen │ Llama: same facts if triage is held fixed)
   rule: status, dates, amounts only from [BUSINESS DATA]; chunks = procedure
    │
    ▼
draft: "Order ORD-9901 is processing ..." + cites the order-status procedure chunk
```

**What to build, in the repo's terms.**

1. **Fetch plan outside the provider.** Put it in a small pure module that the Context Builder calls. The CRM/ERP adapter then only executes lookups, so it stays replaceable without touching the Context Builder (R13.2). Inputs: org, sender email, category, intent, typed IDs, and the routed profile's `context_policy`. Output: a list of lookups with reasons. Record it in the CONTEXT_READY payload, next to `retrieval_performed`.
2. **Typed ID extraction on every job.** Either fix `extract_identifiers` to keep the pattern name and require a digit, or add a separate extractor next to it. It must run even when `retrieval_required` is false. Decide how "order 82915" maps to a stored `order_number` such as `ORD-NNNN` (§4).
3. **One combined provider call**, in the spirit of `get_customer_context` [22]: for example `get_business_context(org_id, sender_email, plan)` returning typed facts. Use only fixed, parameterised queries, never model-written SQL [34][35]. The customer ID always comes from code, never from model output [25]. Every query carries `organization_id` (CLAUDE.md:66).
4. **An explicit status on every fact** (R13.6): FOUND, NOT_FOUND (lookup ran, no row for this customer), NOT_LOOKED_UP (with a reason: not planned, blocked by `context_policy`, or `unsupported_entity` for INV- IDs while no invoice store exists), UNAVAILABLE (timeout or error), UNKNOWN_SENDER, AMBIGUOUS_CUSTOMER. This follows "return an empty result, not a 404" [4] and the evidence that models guess when facts are missing [3][39]. Keep NOT_FOUND and UNAVAILABLE apart, so a timeout never becomes "we have no such order". The `(org, email)` index is non-unique (migrations/0001_core_schema.up.sql:289), so two matching customers must give AMBIGUOUS_CUSTOMER and no lookups, as Salesforce does [18].
5. **Timeout and degraded flag** (R13.7). Give the provider call its own deadline and set the database statement timeout explicitly [58]. On timeout, emit UNAVAILABLE facts, set `business_data_degraded=true` in CONTEXT_READY, and still write the draft [57]. Mirror `retrieval_degraded`. A circuit breaker is optional: Microsoft says it may not suit message-driven systems, where dead-letter queues and retries are often enough [59].
6. **One label and one precedence rule** (R13.3, R13.5). Render facts under `[BUSINESS DATA]` in every Jinja profile, not only billing. Add a source and as-of line, then one `key: value` line per fact. Avoid JSON arrays: JSON-encoded documents "performed particularly poorly" in OpenAI's GPT-4.1 long-context tests [28]. OpenAI suggests XML attributes for metadata [27]; Anthropic suggests `<source>` subtags inside `<document>` tags [23]. Add one instruction, for example: "Order, ticket and invoice status, dates and amounts come only from [BUSINESS DATA]. If a fact is NOT_FOUND or UNAVAILABLE, say so. Knowledge chunks explain procedure only." Keep the knowledge chunks, because the gate needs a procedure citation. This precedence rule is a design choice; no provider document supplies it.
7. **Pass the as-of time and business timezone.** GPT-5.5 knows the current UTC date [29]; that note does not cover the local models.
8. **Record the identity assumption.** Treating the From address as the customer is a trust assumption that no source endorses [5][16][17]. CLAUDE.md:101-107 puts security out of scope and asks you to raise it, not improvise. For matching, lowercase the domain. Lowercasing the local part is a policy choice: RFC 5321 says it is case-sensitive, though relying on that is discouraged [56].

**Spec impact.** Keying the fetch on IDs and category diverges from design §5.4 (specs/design.md:402) and task 5.4 (specs/tasks.md:621). Under CLAUDE.md:111-123 that needs, in order: the owner's decision, a `specs/design.md` update, an ADR in `docs/adr/` (it would be the first; the folder holds only `.gitkeep`), a `specs/tasks.md` sync, then code. R13.3's "WHEN the classified intent requires..." may need a clarifying note. Do not renumber it (CLAUDE.md:125). The same ADR should make `context_policy` the gate for the snapshot, and state that typed-ID lookups override it.

**Gate notes** (specs/tasks.md:634).

- Seed order 82915 for the gate email's sender, or change the gate email to an existing pair (Dana + ORD-9901, or Alice + ORD-8820).
- Edward's email should give NOT_FOUND for ORD-9901 and FOUND for TICK-4402. That is a good negative test for R13.4.
- The gate needs a real model. FakeLLMProvider always returns a fixed reply (packages/llm/fake.py:24), and tests must not need live credentials (CLAUDE.md:131). So the gate is a live run, separate from the test suite. Unit tests cover the plan, statuses and timeout with the fake.

## 3. Q2: the three models

### 3.1 What the sources say

**Serving.**

- Ollama can keep several models loaded at once if they fit in memory. The default limit is 3 per GPU, or 3 on CPU. When memory runs out, new requests queue until a model can load. Models stay loaded for 5 minutes by default, adjustable with `keep_alive` [50]. The request's `model` field picks the model, so one base URL can serve both Qwen and Llama.
- Ollama's OpenAI-compatible endpoint supports `response_format` and `tools`, but not `tool_choice` [49]. Structured outputs work through `response_format`; the native API takes the schema in `format`. Ollama suggests also putting the schema in the prompt and lowering the temperature. Ollama's Cloud does not support structured outputs [48].
- Ollama's default context length depends on VRAM: 4k below 24 GiB, 32k for 24–48 GiB, 256k at 48 GiB or more. It can be raised with `OLLAMA_CONTEXT_LENGTH` [51].
- llama.cpp's server accepts `response_format` with a JSON schema, used for grammar-based sampling [52]. Its router mode serves several models by the `model` field. It was merged on 2025-12-01 as "experimental" [54]. The README lists `--models-max` with a default of 4 [52].
- Hugging Face TGI was archived on 2026-03-21 and is in maintenance mode. Hugging Face now points to vLLM, SGLang, llama.cpp or MLX [55]. For this project, "Hugging Face" means downloading weights to run under Ollama or llama.cpp.

**Model behaviour.**

- Qwen says its tool-call output is "not guaranteed" to follow the protocol, and production code needs countermeasures [45]. The same docs call Qwen3 a reasoning model [45]. Qwen3.5 models think by default [46].
- llama.cpp warns that aggressive KV-cache quantisation such as `-ctk q4_0` can "substantially degrade" tool calling [53].
- BFCL V4 (updated 2026-04-12) measures function calling, not the `response_format` JSON-schema output the repo uses. Treat it as a proxy only. Its rows also come in two modes. FC rows ran with function calling on, and Prompt rows were prompted with system messages [64], so rows in different modes are not like-for-like.
  - Overall: GPT-4.1 (FC) 53.96%, Qwen3-8B (FC) 42.57%, Qwen3-4B-Instruct-2507 (FC) 35.68%, Llama-3.3-70B (FC) 31.90%, Llama-3.1-8B (Prompt, its only row) 25.83%, Llama-3.2-3B (FC) 21.95% [44].
  - Like-for-like (all FC) against the repo's default Llamas: Qwen3-8B 42.57% vs llama3.3:70b's 31.90% and llama3.2:3b's 21.95% [44].
  - Single-turn "Live": Qwen3-8B (FC) 80.53%, GPT-4.1 (FC) 69.95%, Llama-3.2-3B (FC) 58.33%, Llama-3.1-8B (Prompt) 70.76%. Multi-turn: Qwen3-8B 41.75%, GPT-4.1 38.88%, Llama-3.1-8B (Prompt) 11.12%, Llama-3.2-3B 4.00% [44].
  - The BFCL paper says single-turn calls are handled well, while memory and long-horizon decisions remain open [41].
- τ²-bench, GPT-4.1 submission: retail pass^1 74.0, falling to 52.3 at pass^4 (airline 56.0, telecom 34.0). GPT-4.1-mini: retail pass^1 61.4. The leaderboard has no Llama entries and no small local Qwen3 entries [62]. Even the baseline is not consistent across repeated trials.
- Vendor-reported, not independent: Qwen3-4B-Instruct-2507 lists BFCL-v3 61.9 (GPT-4.1-nano 53.0) and TAU2-Retail 40.4 [63]. Qwen3.5-9B lists BFCL-V4 66.1 and TAU2-Bench 79.1 [46]. These come from vendor runs and, for the 4B model, a different BFCL version than the leaderboard's 35.68%, so do not compare them directly with [44]. The Qwen3.5-9B card strongly recommends SGLang, KTransformers or vLLM for production or high-throughput serving [46].
- Constrained decoding scored at least as well as unconstrained decoding in the open-source frameworks tested, llama.cpp included, with gains of up to about 4 points [40].
- A fine-tuned Llama-3-8B beat zero-shot GPT-4o at joint intent and slot filling [43]. Smaller models hallucinate or abstain often even when the context is sufficient [39].
- Prompt format matters more for smaller models. GPT-3.5 varied by up to 40% on one task, while GPT-4 was more robust. Only GPT models were tested [61].
- OpenAI: build a baseline with the most capable model, then try smaller ones against evals [24]. Strict mode for function calling requires `additionalProperties: false` and every field listed as required [25]. Structured Outputs keeps responses to the supplied JSON Schema, with no missing required key and no invented enum value [26].

### 3.2 What the repo allows or blocks

| Need | Repo today | Evidence |
|---|---|---|
| GPT via the OpenAI API | Works: `LLM__PROVIDER=openai` | packages/core/settings.py:216 |
| One base URL for both local models | Yes: `LLM__LOCAL_BASE_URL`, default `http://localhost:11434/v1` | packages/core/settings.py:229; packages/llm/client.py:250; .env.example:87 |
| Run Qwen | **Blocked.** `local`/`ollama`/`vllm` ignore `LLM__FAST_MODEL` and `LLM__STRONG_MODEL` and use hard-coded Llama names | packages/llm/factory.py:112; packages/llm/client.py:209 |
| Default local tiers | llama3.2:3b (routine), llama3.3:70b (high_capability), llama3.2:1b (fallback) | packages/llm/client.py:209 |
| GPT on one tier, local on another | **Blocked.** One `provider` serves all tiers and call kinds | packages/core/settings.py:216 |
| GPT for triage, local for generation | Not by config today. Compose sets one `LLM__PROVIDER` in a shared anchor merged into both workers | docker-compose.yml:16, :227, :266 |
| Structured output on local models | Sends `response_format` `json_schema` with `strict: true` to `/chat/completions`. No Ollama `format` path | packages/llm/client.py:102 |
| Parse of the reply | `json.loads` on `message.content`; failure is a repairable schema error | packages/llm/client.py:167 |
| Model label vs real model | The router's model name is only a label; the local provider's own map picks the model | services/ai_worker/consumer.py:149 |
| Local URL inside containers | Compose passes neither `LLM__LOCAL_BASE_URL` nor the model names into the services, and uses no `env_file` | docker-compose.yml:2-27 |

Three repo risks follow from this. Each needs one live call to settle.

- **Triage schema vs OpenAI strict mode.** The triage schema has no `additionalProperties: false` and leaves `priority` and `reasoning` out of `required` (services/triage_worker/llm_classifier.py:209). OpenAI states those requirements for strict function calling [25]. Whether `response_format` strict mode rejects this schema is not verified here.
- **Qwen thinking output (unverified hypothesis).** Qwen's docs call Qwen3 a reasoning model [45], and Qwen3.5 thinks by default [46]. No source here says what the thinking text looks like on the wire. If it lands in `message.content`, `json.loads` fails and the job spends its one repair call. Whether it does, whether Ollama separates thinking from content, and how to switch thinking off there are not verified here.
- **Context length.** On a GPU under 24 GiB, Ollama defaults to 4k tokens [51]. Instructions, thread, chunks and business facts may not fit. The cited docs do not say what happens then.

### 3.3 Recommendation

**Model per slot.** With pre-fetch, no slot needs to call a tool. The job is to write a schema-valid reply from given facts, so the benchmarks below are proxies. This uses one Qwen generation (Qwen3) throughout. If the controller's Q3 covers model choice, treat this table as input to it.

| Slot | Pick | Tag or setting | Evidence | Caveat |
|---|---|---|---|---|
| GPT (API) | GPT-4.1 as the baseline; GPT-4.1-mini as the cheaper step [24] | `LLM__FAST_MODEL`, `LLM__STRONG_MODEL` (repo defaults gpt-4o-mini and gpt-4o, packages/core/settings.py:236-245) | BFCL V4 FC 53.96%, irrelevance 86.52% [44]; τ²-bench retail pass^1 74.0 → pass^4 52.3; 4.1-mini pass^1 61.4 [62] | The repo defaults have no numbers in this evidence. Account availability of GPT-4.1 was not checked. Because of the pass^k drop, run each gate email several times. |
| Qwen (local) | Qwen3-8B | Ollama tag not verified here; needs item 2 | BFCL V4 FC 42.57%, Live 80.53%, irrelevance 79.07% [44] | Best local row on the same independent leaderboard. A reasoning model [45], so smoke-check that its reply parses. Smaller step: Qwen3-4B-Instruct-2507 (FC 35.68% [44]). |
| Llama (local) | Llama-3.1-8B-Instruct, size-matched to Qwen3-8B (my inference) | Ollama tag not verified here; needs item 2 | BFCL V4 Prompt mode only: 25.83%, irrelevance 42.70% [44] | Not like-for-like with Qwen's FC row. Low-memory fallback: repo default llama3.2:3b (FC 21.95%, irrelevance 52.06% [44]). llama3.3:70b (FC 31.90%) is the repo's high_capability default; use it only if it fits next to the other model [50]. |

Qwen3.5-9B reports higher numbers (BFCL-V4 66.1, TAU2-Bench 79.1 [46]), but they are vendor-reported. It also thinks by default [46], and its runtime support in Ollama and llama.cpp is unchecked (§4). Keep it as a later candidate, and do not mix its numbers with the Qwen3 rows.

1. **Serve both local models through Ollama at one base URL.** It is the simplest option, and it is the repo's default URL. llama.cpp router mode also works but started as experimental [54]. Skip TGI [55].
2. **Make local model names configurable.** This is a small change in the factory: read tier model names from settings for `local`, with the Llama map as the fallback. Without it, Qwen cannot run. It changes config behaviour, so it needs the owner's go-ahead.
3. **Hold triage on one provider when comparing the three models.** The ID lookups are the same for every model. But when Stage 3 decides (ML confidence below 0.80), the category and intent, and so the snapshot, come from the triage model. In a whole-stack run (`openai`; `local` with Qwen; `local` with Llama), that is the model under test, because triage and generation share `settings.llm` (packages/core/settings.py:216; services/triage_worker/main.py:126; services/ai_worker/main.py:83). For a drafting-only comparison, use the per-service split in item 4 with one triage provider for all three runs, and check that the recorded category and intent match across runs. Then each model gets the same `[BUSINESS DATA]` block. If the split is not built, report the three whole-stack runs as comparing triage and drafting together, not reply writing alone. Start with GPT as the baseline [24].
4. **Do not mix GPT and local across routine and high_capability in Phase 5.** That needs a per-tier provider map, which changes the config model behind R14.7. A per-service split (GPT for triage, local for generation) is a smaller step: a service-level override in `docker-compose.yml`. It has not been tested.
5. **Before the gate, run three one-call smoke checks,** one per backend: the triage schema under OpenAI strict mode; Ollama with `json_schema` and `strict: true` as the client sends it; and a Qwen reply that parses. Set `OLLAMA_CONTEXT_LENGTH` explicitly. Pass `LLM__LOCAL_BASE_URL` into the containers if the workers run under compose.
6. **Size the models so both stay loaded** [50]. The default high_capability model, llama3.3:70b, is large; check it fits before relying on escalation. For Phase 5, set both local tiers to the same model in each run, so escalation does not load a second one (my inference; needs item 2). BFCL is a tool-calling proxy, not a JSON-schema test (§3.1), and with pre-fetch its gaps matter less. Smaller models hallucinate or abstain often even with sufficient context [39] (tested on Mistral and Gemma, not Llama), so the gate check should look for the exact status string.
7. **For evaluation,** run Ragas faithfulness as an offline evaluation, not a unit test. Its claim extraction uses an LLM [60], and tests must not need live credentials (CLAUDE.md:131). Give it the `[BUSINESS DATA]` block plus the cited knowledge chunks as context, or score only the status, date and amount claims against the business block. Otherwise the procedure statements the gate requires from chunks count as unsupported and pull the score down. Faithfulness penalises invented facts, not missing ones, so also check that the required status appears in the draft.

## 4. What this does not settle

- Whether OpenAI strict mode rejects the current triage schema, and whether Ollama and llama.cpp honour `strict: true` as sent. One live call each.
- How Ollama returns Qwen3 thinking text (and Qwen3.5's, if it is tried later), and whether Ollama or llama.cpp support the Qwen3.5 architecture at all. Not checked.
- Ollama tags for Qwen3-8B, Qwen3-4B-Instruct-2507 and Llama-3.1-8B-Instruct, and whether GPT-4.1 is available on the project's OpenAI account. Not checked.
- The effect of Q4 GGUF quantisation on JSON-schema accuracy. No primary data was found.
- No study compares always preloading a customer snapshot against ID-triggered lookups. The snapshot's size and which categories get it need a small experiment.
- No provider document states a "system of record beats retrieved text" rule. The precedence instruction is a design choice to test on all three models.
- Placement: Anthropic puts long data first and the query last [23]; OpenAI puts context near the end [27]. R14.8 already fixes the section order. Whether a closing reminder after `[BUSINESS DATA]` helps is untested.
- Invoice scope: there is no invoice table. Whether invoice facts are in Phase 5, or map onto orders, is open. Until then, INV- IDs give NOT_LOOKED_UP (`unsupported_entity`), never NOT_FOUND.
- ID normalisation: how "order 82915" maps to `ORD-NNNN`, and whether order 82915 gets seeded.
- Business fixtures are single-tenant, while CLAUDE.md:134 asks for at least 3 tenants.
- Whether `NormalizedMessage.sender.email` is lowercased at ingestion. Not checked.
- Whether `ThreadState.current_intent` (packages/domain/entities.py:414) is usable as a signal. Not checked.
- Trusting the From address is a security decision outside the brief. It needs the owner's call.

## References

Dates are page dates where shown, otherwise fetch or commit dates. "Undated" pages were read on 2026-09-28.

1. How to set up Data connectors. Intercom. 2026-09-25. https://www.intercom.com/help/en/articles/9916497-how-to-set-up-data-connectors
2. Fin Procedures FAQs. Intercom. Updated week of 2026-09-28. https://www.intercom.com/help/en/articles/13617008-fin-procedures-faqs
3. Fin and Data connectors FAQs. Intercom. 2026-09-10. https://www.intercom.com/help/en/articles/9916507-fin-and-data-connectors-faqs
4. Troubleshooting Fin Procedures and Data connectors. Intercom. 2026-09-25. https://www.intercom.com/help/en/articles/13704396-troubleshooting-fin-procedures-and-data-connectors
5. Best practices when using Data connectors with Fin. Intercom. 2026-05-11. https://www.intercom.com/help/en/articles/9916183-best-practices-when-using-data-connectors-with-fin
6. About generative procedures for AI agents. Zendesk. 2026-07-07. https://support.zendesk.com/hc/en-us/articles/10473649691418-About-generative-procedures-for-AI-agents
7. Examples of generative procedures for AI agents. Zendesk. 2026-06-15. https://support.zendesk.com/hc/en-us/articles/9424547984026-Examples-of-generative-procedures-for-AI-agents
8. About the integration builder for AI agents. Zendesk. 2026-09-22. https://support.zendesk.com/hc/en-us/articles/8357756844442-About-the-integration-builder-for-AI-agents
9. Announcing agentic AI for advanced email AI agents. Zendesk. 2026-04-20 (updated 2026-09-25). https://support.zendesk.com/hc/en-us/articles/10563281043738-Announcing-agentic-AI-for-advanced-email-AI-agents
10. Agent Router, Agent Script, Agentforce Developer Guide. Salesforce. Undated. https://developer.salesforce.com/docs/ai/agentforce/guide/ascript-patterns-topic-selector.html
11. Multi-Surface Customer Support Agent, Agentforce Examples. Salesforce. Undated. https://developer.salesforce.com/docs/ai/agentforce/guide/ascript-examples-customer-support.html
12. Skills explained. Gorgias. About 2026-07. https://docs.gorgias.com/en-US/skills-explained-6556816
13. Use intents and sentiments to prioritize and route tickets. Gorgias. About 2026-07. https://docs.gorgias.com/en-US/use-intents-and-sentiments-to-prioritize-and-route-tickets-81924
14. API tool control. Ada. Undated. https://docs.ada.cx/docs/automation/tools/api-tools/api-tool-control
15. Connect to external systems using API actions (Freshdesk). Freshworks. 2026-06-29. https://support.freshdesk.com/support/solutions/articles/50000011661-api-actions-library-in-freshdesk
16. Workflows for AI Agents in Freshdesk. Freshworks. 2026-08-18. https://support.freshdesk.com/support/solutions/articles/50000011733-workflows-for-ai-agents-in-freshdesk
17. Assigning your Inbox agent to conversations. Shopify. Undated. https://help.shopify.com/en/manual/inbox/assigning-your-ai-staff-member
18. Email-to-Case Contact Matching Logic. Salesforce. 2026-07-21. https://help.salesforce.com/s/articleView?id=000385630&language=en_US&type=1
19. Building effective agents. Anthropic. 2024-12-19, since updated. https://www.anthropic.com/engineering/building-effective-agents
20. Effective context engineering for AI agents. Anthropic. 2025-09-29. https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
21. How tool use works. Anthropic. Undated. https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works
22. Writing effective tools for agents — with agents. Anthropic. 2025-09-11. https://www.anthropic.com/engineering/writing-tools-for-agents
23. Prompting best practices. Anthropic. Undated. https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices
24. A practical guide to building agents (PDF). OpenAI. 2025-04-07. https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf
25. Function calling. OpenAI. Undated. https://developers.openai.com/api/docs/guides/function-calling
26. Structured model outputs. OpenAI. Undated. https://developers.openai.com/api/docs/guides/structured-outputs
27. Prompt engineering. OpenAI. Undated. https://developers.openai.com/api/docs/guides/prompt-engineering
28. GPT-4.1 Prompting Guide (OpenAI Cookbook). OpenAI. 2025-04-14. https://developers.openai.com/cookbook/examples/gpt4-1_prompting_guide
29. Using GPT-5.5 (model guidance). OpenAI. Undated. https://developers.openai.com/api/docs/guides/prompt-guidance?model=gpt-5.5
30. Learning to Route: A Rule-Driven Agent Framework for Hybrid-Source RAG (arXiv 2510.02388). Bai et al. 2025-09-30, revised 2026-02-12. https://arxiv.org/abs/2510.02388 (full text: https://arxiv.org/html/2510.02388)
31. Intent Detection in the Age of LLMs (EMNLP 2024 Industry). Arora, Jain, Merugu. 2024-10-02. https://arxiv.org/abs/2410.01627
32. Are LLMs All You Need for Task-Oriented Dialogue? (SIGDIAL 2023). Hudeček, Dušek. 2023-08-03. https://arxiv.org/abs/2304.06556
33. A Generative Model for Joint Multiple Intent Detection and Slot Filling (arXiv 2602.08322, preprint). Li, Zhu. 2026-02-12. https://arxiv.org/abs/2602.08322
34. From Prompt Injections to SQL Injection Attacks (P2SQL, ICSE 2025). Pedro et al. 2025-01-27. https://arxiv.org/html/2308.01990v4
35. Spider 2.0: Evaluating Language Models on Real-World Enterprise Text-to-SQL Workflows (ICLR 2025). Lei et al. 2025-03-17. https://arxiv.org/abs/2411.07763
36. τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains. Yao et al. (Sierra, Princeton). 2024-06-17. https://arxiv.org/abs/2406.12045
37. Hallucination-Free? Assessing the Reliability of Leading AI Legal Research Tools. Magesh et al. (Stanford). 2024-05-30. https://arxiv.org/abs/2405.20362
38. ClashEval: Quantifying the tug-of-war between an LLM's internal prior and external evidence. Wu, Wu, Zou. 2025-02-07. https://arxiv.org/abs/2404.10198
39. Sufficient Context: A New Lens on RAG Systems. Joren et al. 2025-04-23. https://arxiv.org/abs/2411.06037
40. JSONSchemaBench: A Rigorous Benchmark of Structured Outputs for Language Models. Geng et al. 2025-02-27. https://arxiv.org/html/2501.10868
41. The Berkeley Function Calling Leaderboard (BFCL) (ICML 2025, PMLR 267). Patil et al. 2025. https://proceedings.mlr.press/v267/patil25a.html
42. Enhancing Customer Service Chatbots with Context-Aware NLU (CODS-COMAD 2024). Nandi et al. (Walmart). 2024-12 (arXiv 2025-06-02). https://arxiv.org/abs/2506.01781
43. Fine-Tuning Medium-Scale LLMs for Joint Intent Classification and Slot Filling (COLING 2025 Industry). Aguirre et al. 2025. https://aclanthology.org/2025.coling-industry.21/
44. BFCL V4 leaderboard data (data_overall.csv). UC Berkeley Gorilla. 2026-04-12. https://gorilla.cs.berkeley.edu/data_overall.csv
45. Function Calling, Qwen docs. Qwen team (Alibaba). 2025-06-09. https://qwen.readthedocs.io/en/latest/framework/function_call.html
46. Qwen/Qwen3.5-9B model card. Qwen team (Alibaba). 2026-02. https://huggingface.co/Qwen/Qwen3.5-9B
47. Llama 3.1 prompt format (meta-llama/llama-models). Meta. Undated. https://raw.githubusercontent.com/meta-llama/llama-models/main/models/llama3_1/prompt_format.md
48. Structured Outputs. Ollama. Undated. https://docs.ollama.com/capabilities/structured-outputs
49. OpenAI compatibility. Ollama. Undated. https://docs.ollama.com/api/openai-compatibility
50. FAQ. Ollama. Undated. https://docs.ollama.com/faq
51. Context length. Ollama. Undated. https://docs.ollama.com/context-length
52. LLaMA.cpp HTTP Server README. ggml-org. Master, read 2026-09-28. https://raw.githubusercontent.com/ggml-org/llama.cpp/master/tools/server/README.md
53. Function calling (docs/function-calling.md). ggml-org llama.cpp. 2026-05-22. https://raw.githubusercontent.com/ggml-org/llama.cpp/master/docs/function-calling.md
54. server: introduce API for serving / loading / unloading multiple models (PR #17470). ggml-org llama.cpp. Merged 2025-12-01. https://api.github.com/repos/ggml-org/llama.cpp/pulls/17470
55. text-generation-inference repository (archived). Hugging Face. 2026-03-21. https://github.com/huggingface/text-generation-inference
56. RFC 5321, Simple Mail Transfer Protocol, section 2.4. IETF. 2008-10. https://www.rfc-editor.org/rfc/rfc5321#section-2.4
57. REL05-BP01 Implement graceful degradation. AWS Well-Architected. Undated. https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_mitigate_interaction_failure_graceful_degradation.html
58. REL05-BP05 Set client timeouts. AWS Well-Architected. Undated. https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_mitigate_interaction_failure_client_timeouts.html
59. Circuit Breaker pattern. Microsoft Azure Architecture Center. 2025-02-05 (updated 2026-09-26). https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker
60. Faithfulness, Ragas documentation. Ragas. 2025-12-09. https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/
61. Does Prompt Formatting Have Any Impact on LLM Performance? (arXiv 2411.10541). He et al. 2024-11-15. https://arxiv.org/abs/2411.10541
62. tau2-bench leaderboard submission: gpt-4-1_openai (submission.json). Sierra Research. Submission date 2025-06-09. https://raw.githubusercontent.com/sierra-research/tau2-bench/main/web/leaderboard/public/submissions/gpt-4-1_openai_2024-06-20/submission.json
63. Qwen/Qwen3-4B-Instruct-2507 model card (vendor-reported). Qwen team (Alibaba). Undated. https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507
64. Berkeley Function Calling Leaderboard (blog). UC Berkeley Gorilla. Last updated 2024-08-19. https://gorilla.cs.berkeley.edu/blogs/8_berkeley_function_calling_leaderboard.html

## Appendix: Dropped by verification

No claim was dropped outright. The fact-checker refuted or could not confirm these parts of kept claims. None of them is used as evidence above.

- tau2-bench: "the only Qwen entries are Qwen3-max, Qwen3.5-397B and RAFT" was refuted; the folder also holds Qwen 3.8 Max and qwen3.5-omni entries.
- Intercom: a 30 s connector timeout inside Procedures applies only "in eligible workspaces", not everywhere.
- Intercom: "an email sender address is not a verified identity" is an inference; the page never mentions email.
- Zendesk: "the lookup is gated by the classified intent" is an inference; the page does not define a use case as an intent.
- Gorgias: "behaviour is driven by the classified intent" is overstated; actions can also run from guidance.
- Ada: "LLM-chosen trigger" holds only after the default checkbox is cleared.
- Salesforce: "without waiting for the LLM to choose" is the claimant's wording; the April 2026 rename and Spring '26 reference were not confirmed.
- Anthropic: the customer-support fit and the "compounding errors" warning come from different sections.
- Anthropic: "needs a tool to fetch it" is not an argument against code-side pre-retrieval.
- Anthropic: tool_use blocks in responses are output tokens, not billed input.
- OpenAI: "build an agent only where rules fail" is stronger than the source's "prioritize" and "may suffice".
- Learning to Route: "always adding both sources hurts" is stronger than the paper's "may fail".
- Adaptive-RAG: does not beat the fixed multi-step strategy on accuracy (37.17 vs 39.00 EM).
- P2SQL: "the LLM never writes the query" overstates a defence scoped to user-specific data.
- Spider 2.0: "free-form SQL is unreliable on real schemas" is an inference from enterprise-scale databases.
- ClashEval: "a stale order status would be believed" is an extrapolation not in the paper.
- JSONSchemaBench: "higher in every framework" is wrong; llama.cpp tied on one task, and OpenAI and Gemini were not in the quality test.
- Alizadeh et al.: "no defence stopped leakage" holds only for the 48-task set; some defences reached zero in the 16-task set.
- vLLM: "only `required` or a named function constrains calls" is overstated; per-tool `strict` and a server floor also do.
- Ollama: "a client cannot forbid a tool call" is overstated; the client can omit `tools`.
- TGI: "one model per server" is inferred from a quick-start example.
- BFCL blog: "relevance" and "irrelevance" were merged; only Irrelevance means no call is expected.
- He et al.: the 40% figure comes from one code-translation task, GPT models only.
- RFC 5321: "lowercasing the local part is widely tolerated" is not in the RFC.
- Ollama context: "a long prompt will not fit" is an inference; the page does not say what happens.
- llama.cpp: the `truncated` flag is documented only for `/completion`, not every endpoint.
- OpenAI GPT-5.5 guidance says "should", not "must".
- Ragas: the HHEM option sits under the legacy metrics API.
- Unverified, not cited: Meta's line "Llama 8B-Instruct can not reliably maintain a conversation alongside tool calling definitions" (page behind SSO).
- Unverified, not cited: that llama.cpp silently skips JSON-schema features it does not support.
- Left out for lack of public docs: Sierra, Decagon, Semantic Router.

## Summary

**Problem:** Phase 5 must fetch live order and ticket facts "when the intent requires it", but no intent names them and the ML stage sends no intent at all, so an intent-only trigger would rarely fire.
**Recommendation:** Decide the fetch in code before the one generation call, from sender, typed IDs, the profile's `context_policy` and intent, with an explicit status on every fact. Serve Qwen3-8B and Llama-3.1-8B-Instruct from one Ollama URL once local model names are configurable, with GPT-4.1 as the baseline. Compare drafting with triage held on one provider, or label whole-stack runs as triage plus drafting.
