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

Guard v3 (ADR-0015, `docs/adr/0015-guard-architecture-v3.md` in the host repository) has four layers named
by meaning and one prompt-building switch that is not a layer. Every stage consumes and produces typed
contracts in `mailguard/contracts/`:

| Stage | Input | Output | Fail-closed behaviour |
|---|---|---|---|
| L1 `InboundGuard.inspect` (`layers/inbound_guard`) | `GuardedEmail` | one `InboundVerdict`: severity and score from the **scan** sub-stage only, findings of both sub-stages (tagged `stage`), indicators, sanitized body, intent, actions, entities, stripped segments, question sentences | scan exception -> HIGH severity verdict; extract exception -> empty sanitized body + HIGH; judge failure -> see below |
| L2 `DocScanner.scan` (`layers/doc_scanner`) | `SourceDocument[]`, the email's question sentences, the query | `DocumentVerdict` with one `ChunkVerdict` per chunk (`trust`, `cluster_id`, `signals`), kept and quarantined ids | exception -> chunk quarantined |
| L3 `InboundGate.decide` (`layers/inbound_gate`) | `GuardReport`, category, gate pass, `final` | `PolicyDecision` (action, tier, winning rule, matched rules, audit id) | layer error -> `human_approval` (P00), unless a stricter rule also matches |
| channel isolation `ChannelIsolation.build` (`prompting/`, **not a layer**) | system/category/business + intent + email + kept chunks with their trust | `SecurePrompt` + `IsolationInfo` (never a verdict) | failure -> prompt with the trusted instructions only, `IsolationInfo.error` set (policy fact `layer_error`) |
| L4 `OutputGuard.inspect` (`layers/output_guard`) | `GuardReport`, `DraftCandidate`, kept chunks, protected texts | `(OutputVerdict, PolicyDecision)`: redacted text, redactions, compliance flags, and the final decision with the inbound floor | exception -> empty redacted text + HIGH |

The policy engine (`mailguard/policy/engine.py`) is shared infrastructure, not a layer: the Inbound Gate runs
it at stage `inbound`, the Output Guard at stage `outbound`.

`MailGuardPipeline` orchestrates them in this order (`pipeline.py`):

1. **Inbound:** L1 -> L3 gate pass 1 on L1's facts alone (the documents are marked "not scanned"). A block or
   quarantine stops the job here: `report.doc_scanner` stays `None`, no document judge call is made, and
   `run()` returns `(report, None, None)`; a host files the holding draft of `build_escalated_draft`
   (`action="escalate"`, a fixed body that never quotes the email, no model call).
2. Otherwise L2 (skipped for an empty document set) -> L3 gate pass 2 on every fact. Only the final pass is
   audited; its `audit_id` seed includes the pass number, and `metadata.gate_passes` is 1 or 2.
3. **Prompt:** channel isolation builds the prompt from the chunks L2 kept and their trust.
4. **Outbound:** after the host's one generation call, L4 scans the draft, decides with the outbound policy and
   merges the inbound decision as a floor (a specific rule stricter than `auto_send`; strictest action wins;
   `requires_human` is the OR). With the Output Guard off, `report.decision` stays the inbound decision.

Exit points of a job (used by the benchmark): `gate_pass_1`, `gate_pass_2`, `output_guard`, `delivered`.
The question sentences L2 compares against are a pure function the pipeline always runs, not part of L1, so
switching L1 off does not weaken L2. At most five guard model calls are made per job (`MAX_GUARD_CALLS_PER_JOB`
in `llm/judge_calls.py`: stages `inbound_scan`, `inbound_extract`, `doc_chunk`, `doc_set`, `output`); the
production default enables two, and the count is in `report.metadata["guard_calls"]`.

### Judge failures: fail closed by default

The five judges (the L1 scan judge and extractor, the L2 chunk and set judges, the L4 output judge) each have a
cheap rule/classifier result and an optional AI step. When a **configured** judge fails (timeout, transport
error, non-JSON, schema failure after one repair, invalid fields), the layer keeps its cheap result and records
the failure in the verdict `metadata`, with the same keys for every stage:

| Key | Value |
|---|---|
| `llm_fallback` | `true` (absent when the AI step answered) |
| `llm_fallback_reason` | `timeout` (provider timeout) / `non_json` (prose instead of a JSON object) / `schema_missing` (JSON without the schema's required fields) / `invalid_fields` (null, mistyped or out-of-range fields) / `error` (transport or any other failure) |
| `llm_error` | the exception text, at most 200 characters |

With `MAILGUARD__FAIL_CLOSED_ON_JUDGE_ERROR=true` (the default) the verdict's `error` is also set to
`judge_invalid:<stage>` (`llm/judge_calls.py::judge_failure`), the policy fact `layer_error` fires rule P00
(`human_approval`), and a reviewer sees the email. A judge that is **not configured** (no model, switch off,
unknown model) is not an error and degrades silently to the cheap stages. `false` restores the v2 visible
fallback (the error stays empty and the email is processed normally); the benchmark's `--judge-fallback` selects
it to compare with v2. A failed answer is never partially trusted: the model's schema is validated once, with
one repair attempt (`mailguard/llm/structured.py`), and only a fully valid answer is used. Fields that the
judgement depends on have no defaults in any stage's schema, so `{"raw_text": ...}` or a partial object cannot
pass as "the model found no problem".

The L1 scan judge, the L2 chunk and set judges and the L4 output judge catch `LLMError` (including
`LLMTimeoutError` and schema failures); the L1 extract sub-stage still catches any exception of its AI step
(non-`LLMError` ones are logged with a trace). Any other exception reaches the layer's outer handler and fails
closed (HIGH, `error` set). Consumers of the verdicts (benchmark harnesses) count `llm_fallback` /
`llm_fallback_reason` per stage as the fallback rate and treat `judge_invalid:<stage>` as a fail-closed event
(a quarantine caused only by it is not a detection). `llm_error` is free text for diagnosis.

### Served-provider provenance (OpenRouter route)

`OpenAIProvider` can serve a judge through OpenRouter with one provider pinned. A model spec in
`configs/models.yaml` adds `provider_routing` (OpenRouter's `provider` request object, for
example `order: [coreweave]`, `allow_fallbacks: false`, `require_parameters: true`; only the keys
OpenRouter documents are accepted) and `response_metadata: true`, which sends
`X-OpenRouter-Metadata: enabled`. Each `LLMResult` then carries `provenance`
(`mailguard/llm/provenance.py`): the requested model, the provider that served the call, the
attempt number (above 1 means the router fell back), the generation id, tokens, cost and finish
reason. The provider does not judge the match: a judge stage that raised on a mismatch would only
fall back (and, fail-closed, send the email to review) instead of marking the case an error. The caller that pinned the provider reads `provenance` and treats a
different provider, an attempt above 1 or a response that names no provider as an error of the case.
The provider is read from `openrouter_metadata` first; when that is absent, from the top-level
`provider` field of the response body. `provenance.provider_source` records which one was used.

## Layer design notes

### L1 Inbound Guard
One facade, two sub-stages, one `InboundVerdict` (`layers/inbound_guard/guard.py`). Both share one rule engine and
one classifier; at most two model calls per email.

*scan* (severity and score come only from here):
* Stage 1 rules (`configs/injection_rules.yaml`): 15 rule families, each pattern a regex with a fixed prior
  score (0.55-0.97) and a technique tag. Heuristics add zero-width characters, homoglyph mixing, imperative
  density and hidden HTML.
* Stage 2 classifier: TF-IDF word 1-2 grams + char_wb 3-5 grams -> logistic regression with Platt calibration,
  trained on ~20k labelled texts from 10 public sources (`training/`). Test split (3,662 texts): precision
  0.966, recall 0.927, F1 0.946, AUROC 0.990, FPR 2.0 %, 0.945 ms/text on CPU
  (`artifacts/models/l1_injection_clf_v1.metrics.json`).
* Stage 3 LLM judge: only when the noisy-OR fusion of rules and ML lands in the uncertain band
  `[flag-0.30, block)`. The email is wrapped in nonce markers so the judge itself is not trivially injectable.
  Final score = 0.6 * judge + 0.4 * cheap fusion.

*extract* (adds findings and facts, never raises severity, ADR-0015 decision 9): paragraph/sentence segmentation
preserving offsets; each segment scored by the rules and the classifier; segments >= `strip_threshold` are removed
and recorded with provenance. Heuristic entity/action extraction always runs; the optional LLM paraphrase sees
only the sanitized body and is told to describe, never to follow. A failed AI step follows "Judge failures"
above.

### L2 Doc Scanner
Per-chunk cheap stage: (a) the L1 rules, (b) poisoning heuristics (answer forcing, "ignore other documents",
"this document has priority", contact/link steering), (c) the L1 classifier, fused by noisy-OR. On top, signals
against the email's **question sentences** (`prompting/questions.py`): `echo_vs_question` (token and word-3-gram
overlap of the chunk's opening window with a question, threshold `DOC_SCANNER__ECHO_THRESHOLD` 0.90, questions of
at least 4 content tokens; short questions only as an exact phrase near the start) and `ordered_prefix` (the
chunk opens by repeating a question in order, the PoisonedRAG layout), and bait clusters (chunks that echo the
question and each other are quarantined whole). A chunk >= `quarantine_threshold` or hit by a signal is
quarantined; every chunk gets a `trust` (`trusted`, `untrusted` inside `DOC_SCANNER__SUSPICIOUS_BAND`,
`quarantined`), its raw `signals` and a `cluster_id`. Optional LLM chunk judge for uncertain chunks only (off by
default); optional LLM **set judge** (`DOC_SCANNER__SET_JUDGE_LLM_ENABLED`, off): one call, only when a kept chunk
is in the suspicious band, and it can only add quarantines. The thresholds were chosen on a separate dev split
(`datasets/splits/rag_poison_dev.json`), never on the benchmark's pinned cases. An empty document set is a
no-op. If at least 60 % of the retrieved set is quarantined, the gate escalates to human approval (P04).

### Channel isolation (not a layer)
Spotlighting (Hines et al., 2024) with three modes: `delimit` (nonce-tagged markers), `datamark` (whitespace ->
marker char), `encode` (base64). Untrusted text is scrubbed of forged markers and chat-template tokens and
truncated to a token budget; kept chunks are rendered with their trust level. It returns the prompt and an
`IsolationInfo`, never a verdict: forged markers are graded where they arrive (the email by L1, documents by L2),
and those scrubbed from thread history are recorded in `IsolationInfo.forged_markers`. `enabled=False` (presets
C0, C1 and C3-ISO) skips markers and preamble, but scrub and the token budget still apply.

### L3 Inbound Gate
The shared policy engine at stage `inbound`, called twice (pass 1 on L1's facts, pass 2 on every fact); see the
policy notes below. A block or quarantine stops the job before any model call.

### L4 Output Guard
Regex patterns for secrets and PII (`configs/pii_patterns.yaml`, Luhn-validated cards), n-gram overlap with
protected texts (system prompt), citation integrity against the kept chunks, injected-goal compliance (attacker
emails / URLs / instruction 6-grams from L1 and quarantined chunks appearing in the draft), unsafe actions
(forward, recipients outside the conversation) and external links. Values already supplied by the customer are not
treated as disclosures. Then the outbound policy decision and the inbound floor (see `output_guard/guard.py`):
the floor exists only when the inbound decision came from a specific rule and is stricter than `auto_send`; the
final action is the strictest of the two; the auto-send allowlist is applied after the merge.

### Policy engine (shared)
`configs/policy.yaml` rules evaluated over facts derived from the report (max severity, threat types, layer
errors, quarantine ratio, removed ratio, compliance, citation mismatch, redactions, stage, category, draft action).
All matching rules are collected and the **strictest action wins**
(`quarantine > block > human_approval > draft_only > auto_send`); equal actions go to the lowest priority number.
So a layer error (`P00`, human approval) does not hide a critical injection (`P01`, quarantine), and
human-approval rules (`P04`/`P05`) do not hide an unsafe forward (`P06`, block). A rule with an empty `when`
(`P99-default`) is only the fallback for "nothing else matched", so it never out-ranks a specific rule such as
the clean `auto_send`. The decision records the winning rule (`matched_rule_id`) and every matched rule
(`matched_rule_ids`); the policy version is `2026.10-v3` (the rules are unchanged from `2026.09-v2`).
`auto_send` is only possible for allow-listed categories with a clean outbound report and no floor. Decisions are
deterministic and carry a stable `audit_id`; the JSONL audit log stores ids, scores, the winning and matched rule
ids and hashes only. Environment sections: `INBOUND_GUARD__`, `DOC_SCANNER__`, `CHANNEL_ISOLATION__`,
`OUTPUT_GUARD__`, `POLICY__`, `GUARD_MODELS__` (the v2 `L1__` ... `L5__` keys are refused with a migration table).

## Ablation configurations

| Config | L1 Inbound Guard | L2 Doc Scanner | L3 Inbound Gate | L4 Output Guard | Channel isolation |
|---|---|---|---|---|---|
| C0 | - | - | - | - | - |
| C1 | x | - | x | - | - |
| C2 | x | x | x | - | x |
| C3 | x | x | x | x | x |
| C3-L1 | - | x | x | x | x |
| C3-L2 | x | - | x | x | x |
| C3-L3 | x | x | - | x | x |
| C3-L4 | x | x | x | - | x |
| C3-ISO | x | x | x | x | - |

`C0` here is the guard's preset with every switch off (`GuardConfig.preset("C0")`); the root benchmark also runs
`C0T` (this preset's prompt template) next to `C0` (rag-email's own path, no guard code), see
`evaluation/mailguard_bench` in the host repository.

## Integration points with the core

* `services/guard_worker` consumes `mailguard.inbound` jobs and publishes decisions.
* `mailguard.integration.GuardedReplyAgent` wraps the core's `LLMProvider` and `ContextPackage`.
* The Draft Store & Dispatcher must call `dispatch_allowed(report, recipients)` before send.
