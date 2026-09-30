# AgentMailGuard architecture

## Threat model

Target: an LLM customer-support agent with (1) an email API (read / draft / send), (2) a RAG
knowledge base, (3) a dispatcher that executes email actions. The attacker can send emails
to the support address and can plant documents in the knowledge base; the attacker cannot
modify system prompts or model weights and may adapt to known defenses (paraphrase,
obfuscation, multilingual). Attack objectives (Table II of the paper): override the system
instruction, exfiltrate data through the email tool, manipulate answers through poisoned
chunks, leak system information in the output.

## Data flow and layer contracts

Every layer consumes and produces typed contracts in `mailguard/contracts/`:

| Stage | Input | Output | Fail-closed behaviour |
|---|---|---|---|
| L1 `EmailInjectionScanner.inspect` | `GuardedEmail` | `LayerVerdict` (score, severity, findings, indicators, injected instructions) | exception -> HIGH severity verdict |
| L2 `UserIntentExtractor.extract` | `GuardedEmail` | `SanitizedIntent` (sanitized body, intent, actions, entities, stripped segments) | exception in the rule/classifier step -> empty sanitized body + HIGH; failure of the AI step -> heuristic result kept, `llm_fallback` metadata (see below), no `error` |
| L3b `RetrievedDocumentScanner.scan` | `RetrievedChunk[]`, query | kept chunks + `ChunkVerdict[]` | exception -> chunk quarantined |
| L3 `ChannelIsolation.build` | system/category/business + intent + email + chunks | `SecurePrompt` + `LayerVerdict` | forged markers -> MEDIUM finding |
| L4 `OutputScanner.inspect` | `DraftCandidate`, allowed citations, protected texts, indicators | `OutputVerdict` (redacted text, redactions, compliance flags) | exception -> empty redacted text + HIGH |
| L5 `PolicyEngine.decide` | `GuardReport`, stage, category, draft action | `PolicyDecision` (action, tier, winning rule, matched rules, audit id) | layer error -> `human_approval`, unless a stricter rule also matches |

`MailGuardPipeline` orchestrates them as inbound -> prompt -> outbound and stops before
generation when the inbound decision is `block` or `quarantine`.

### Visible fallback of the AI stages

L1 (judge), L2 (extractor), L3b (document judge) and L4 (output judge) each have a cheap
rule/classifier result and an optional AI step. When the AI step fails, the layer keeps its
cheap result exactly as it would be without the AI step, leaves the verdict's `error` empty
(the email is processed normally, not failed) and records the failure in the verdict
`metadata`, with the same keys for every stage:

| Key | Value |
|---|---|
| `llm_fallback` | `true` (absent when the AI step answered) |
| `llm_fallback_reason` | `timeout` (provider timeout) / `non_json` (prose instead of a JSON object) / `schema_missing` (JSON without the schema's required fields) / `invalid_fields` (null, mistyped or out-of-range fields) / `error` (transport or any other failure) |
| `llm_error` | the exception text, at most 200 characters |

A failed answer is never partially trusted: the model's schema is validated once, with one
repair attempt (`mailguard/llm/structured.py`), and only a fully valid answer is used.
Counting `llm_fallback` per layer over a run gives the fallback rate of each AI stage. L2's
`ExtractorOutput` has no field defaults, so `{"raw_text": ...}` or a partial object cannot
pass as "the model found no instructions".

## Layer design notes

### L1 Email Injection Scanner
* Stage 1 rules (`configs/injection_rules.yaml`): 13 rule families, each pattern a regex with
  a calibrated prior score (0.55-0.97) and a technique tag. Heuristics add zero-width
  characters, homoglyph mixing, imperative density and hidden HTML.
* Stage 2 classifier: TF-IDF word 1-2 grams + char_wb 3-5 grams -> logistic regression with
  Platt calibration, trained on ~20k labelled texts from 10 public sources (`training/`).
  Test F1 0.93, AUROC 0.99, FPR 1.4 %, 0.75 ms/text on CPU.
* Stage 3 LLM judge: only when the noisy-OR fusion of rules and ML lands in the uncertain
  band `[flag-0.30, block)`. The email is wrapped in nonce markers so the judge itself is not
  trivially injectable. Final score = 0.6 * judge + 0.4 * cheap fusion.

### L2 User Intent Extractor
Paragraph/sentence segmentation preserving offsets; each segment scored by the rules and
the classifier; segments >= `strip_threshold` are removed and recorded with provenance.
Heuristic entity/action extraction always runs; the optional LLM paraphrase sees only the
sanitized body and is told to describe, never to follow. If the AI step fails for any reason
the rule-and-classifier result stands and the failure is recorded as `llm_fallback` metadata
(see "Visible fallback of the AI stages").

### L3 Channel Isolation
Spotlighting (Hines et al., 2024) with three modes: `delimit` (nonce-tagged markers),
`datamark` (whitespace -> marker char), `encode` (base64). Untrusted text is scrubbed of
forged markers and chat-template tokens and truncated to a token budget. `enabled=False`
produces the plain concatenation used by the C0/C1 baselines.

### L3b Retrieved Document Scanner
Per-chunk fusion of (a) L1 rules, (b) poisoning heuristics: answer forcing ("when asked X,
answer Y"), "ignore other documents", "this document has priority", contact/link steering,
and query echo (PoisonedRAG prepends the target question), (c) the L1 classifier, and (d) an
optional LLM judge. Chunks >= `quarantine_threshold` are removed; if > 60 % of the retrieved
set is quarantined the policy escalates to human approval.

### L4 Output Scanner
Regex patterns for secrets and PII (`configs/pii_patterns.yaml`, Luhn-validated cards),
n-gram overlap with protected texts (system prompt), citation integrity against the kept
chunks, injected-goal compliance (attacker emails / URLs / instruction 6-grams from L1, L2
and quarantined chunks appearing in the draft), unsafe actions (forward, recipients outside
the conversation) and external links. Values already supplied by the customer are not
treated as disclosures.

### L5 Email Policy Engine
`configs/policy.yaml` rules evaluated over facts derived from the report (max severity,
threat types, layer errors, quarantine ratio, removed ratio, compliance, citation mismatch,
redactions, stage, category, draft action). All matching rules are collected and the
**strictest action wins** (`quarantine > block > human_approval > draft_only > auto_send`);
equal actions go to the lowest priority number. So a layer error (`P00`, human approval) no
longer hides a critical injection (`P01`, quarantine), and human-approval rules (`P04`/`P05`)
no longer hide an unsafe forward (`P06`, block). A rule with an empty `when` (`P99-default`)
is only the fallback for "nothing else matched", so it never out-ranks a specific rule such
as the clean `auto_send`. The decision records the winning rule (`matched_rule_id`) and every
matched rule (`matched_rule_ids`); the policy version is `2026.09-v2` (v1 was first-match by
priority). `auto_send` is only possible for allow-listed categories with a clean outbound
report. Decisions are deterministic and carry a stable `audit_id`; the JSONL audit log stores
ids, scores, the winning and matched rule ids and hashes only.

## Ablation configurations

| Config | L1 | L2 | L3 | L3b | L4 | L5 |
|---|---|---|---|---|---|---|
| C0 | - | - | - | - | - | - |
| C1 | x | - | - | - | - | x |
| C2 | x | x | x | - | - | x |
| C3 | x | x | x | x | x | x |
| C3-Lk | C3 with layer k removed |

## Integration points with the core

* `services/guard_worker` consumes `mailguard.inbound` jobs and publishes decisions.
* `mailguard.integration.GuardedReplyAgent` wraps the core's `LLMProvider` and `ContextPackage`.
* The Draft Store & Dispatcher must call `dispatch_allowed(report, recipients)` before send.
