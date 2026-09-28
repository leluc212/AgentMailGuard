# ADR-0008: Business data is fetched by a code-side plan, not by intent alone or model tool calls

- **Status:** Accepted
- **Date:** 2026-09-28
- **Decided by:** project owner, on the recommendation in `artifacts/superpowers/2026-09-28-phase5-business-data-trigger-research.md`
- **Requirements:** R13.2, R13.3, R13.4, R13.5, R13.6, R13.7, R14.9
- **Changes:** `specs/design.md` §5.4 ("Business data (R13)"), §12.2, §13.3; `specs/tasks.md` Phase 5

## Context

Phase 5 adds live transactional facts (orders, tickets) to the reply context. The design said: "resolve sender → customer, then fetch only the entities the intent names". Reading the code showed that rule cannot work as written:

- No intent in the taxonomy names order status or ticket status (`packages/domain/taxonomy.py`, `config/categories.yaml`).
- The ML triage stage always emits `intent=None` (`services/triage_worker/classifier.py:187`). Every email the ML stage decides would reach the Context Builder with nothing to key a lookup on.
- The LLM stage's intent is a free string that nothing checks against the taxonomy (`services/triage_worker/llm_classifier.py:70`).
- A per-profile switch for business data already exists (`context_policy: thread_plus_rag_plus_business` on the billing profile), but no code reads it.

Two common industry patterns were considered (sources in the research note): an intent or procedure gate that calls the integration (Zendesk, Gorgias, Salesforce), and letting the model choose a lookup tool (Intercom data connectors, Ada API tools). Provider documentation from OpenAI and Anthropic confirms that a model-chosen tool call needs a second model request to use the result.

## Decision

The decision to fetch is made **in code, before the one generation call**, by a pure `FetchPlan` function with these inputs: organization, sender address, the routed profile's `context_policy`, the intent (when there is one) and typed identifiers extracted from the email.

1. Typed order and ticket references (`ORD-…`, "order 82915", `TICK-…`, "ticket 4402") are always looked up, scoped to the sender's customer record. They override `context_policy`. A bare reference needs at least 4 digits, so prose such as "an order 2 days ago" is not a reference.
2. A small snapshot is fetched when the profile's `context_policy` allows business data (recent orders and open tickets), or when the intent is in the fixed `INTENT_ENTITIES` map (only the mapped entities). Its size is configured by `BUSINESS_DATA__SNAPSHOT_ORDERS` / `BUSINESS_DATA__SNAPSHOT_TICKETS`.
3. Invoice references are recorded as `NOT_LOOKED_UP` (`unsupported_entity`), because there is no invoice table.
4. Statuses have two levels, so each case has one representation. Customer resolution is recorded once as `customer_status` (`FOUND`, `UNKNOWN_SENDER`, `AMBIGUOUS_CUSTOMER`, `UNAVAILABLE`). Each planned fact carries `FOUND`, `NOT_FOUND`, `NOT_LOOKED_UP` (with a reason) or `UNAVAILABLE`.
5. Lookups run behind `BusinessDataProvider` with a timeout. A timeout degrades to `UNAVAILABLE` statuses plus a recorded `business_data_degraded` flag, and the draft is still written.
6. Facts render in one `[BUSINESS DATA]` section with a precedence rule: statuses, dates and amounts come only from that section, and knowledge chunks explain procedure.

The intent is one input among several. R13.3 still holds: whenever the intent requires transactional facts, they are fetched. The plan adds deterministic triggers on top of it.

## Identity assumption

The sender's From address is treated as the customer's identity, as R13.4 requires ("resolve the sender to a customer record where possible"). No source consulted treats an email From address as verified identity. Sender verification is an authentication concern, which is deliberately out of scope (GEMINI.md §6). This ADR records the assumption; it does not add a control. Matching is case-insensitive on the whole address within one organization. Two matching customers give `customer_status=AMBIGUOUS_CUSTOMER`, and every planned fact is `NOT_LOOKED_UP` (`ambiguous_customer`).

## Consequences

- Works for rule-, ML- and LLM-decided emails alike.
- Typed-ID lookups involve no model, so they are identical whichever model drafts the reply. The snapshot follows the triage result, so it depends on the triage model when the LLM stage decides.
- No extra model calls. The per-job budget (≤1 triage + ≤1 summarization + 1 generation + ≤1 repair) is unchanged.
- A question with no typed ID ("where is my stuff?") gets only the snapshot, and only if its category's profile allows business data. Today that is billing only. Turning it on for other profiles is a configuration change.
- `context_policy` stops being a decorative field: it now gates the snapshot.
- Template replies stay zero-lookup in Phase 5.
- Products are not looked up in Phase 5. `product` and `order_item` are seeded (R13.1), but no product or SKU facts reach `[BUSINESS DATA]` (owner decision 2026-09-28).
- Accepted edges: a bare "order 2026-09-20" plans `ORD-2026` (answered `NOT_FOUND`), and a reply whose subject still names an order plans that order again.

## Alternatives rejected

- **New `order_status` / `ticket_status` intents as the only trigger.** Closest to the old wording, but misses every ML-decided email and needs retrained ML labels. Adding the intents later can still feed the same plan through the intent→entity map.
- **`data_needs` / `entity_refs` fields in the triage output.** Better for mixed and ID-less emails, but only emails that reach the LLM stage get them. It remains a candidate upgrade if evaluation shows ID-less questions being missed.
- **Model tool calling during generation.** Needs a second model request per lookup, which breaks the one-generation-call rule (R14.9). It also leans on the weakest skill of small models (deciding when not to call a tool).
