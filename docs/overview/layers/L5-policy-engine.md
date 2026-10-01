# L5 — Policy Engine

Reference card for layer 5 (L5) of AgentMailGuard, the security package that wraps rag-email's reply writer.

**Basis.** Code read at commit `81df5d07` of AgentMailGuard-bench. Line numbers refer to that worktree unless a path starts with `dazzling-bose/`. Benchmark numbers come from the v1 runs `2026-09-29-gpt4omini` (gpt-4o-mini) and `2026-09-29-qwen25` (qwen2.5:7b-instruct), configuration C3 (all layers on). Where the code or the evidence did not settle a detail, this card says "not determined".

**Terms used below.**

- **Layers.** L1 is the injection scanner, L2 the intent extractor and sanitizer, L3 channel isolation, L3b the retrieved-document scanner, L4 the output scanner.
- **Verdict.** The result one layer reports: a severity, a score and a list of findings.
- **Severity.** One of none, low, medium, high, critical.
- **Threat type.** A label on a finding, such as `data_exfiltration` or `prompt_injection`.
- **Quarantine.** For L3b, removing a retrieved knowledge chunk from the prompt. For L5, the strictest action.
- **RAG.** Retrieval-augmented generation: the reply model is shown chunks of company documents. RAG poisoning means an attacker planted instructions in those documents.
- **Fail-closed.** On an internal error, choose the cautious outcome instead of the permissive one.
- **Facts.** The summary values L5 computes from the other layers' verdicts.

## 1. Purpose

L5 turns the verdicts of the other guard layers into one action (`quarantine`, `block`, `human_approval`, `draft_only` or `auto_send`), using an ordered list of YAML rules. It detects nothing itself.

## 2. Input

The entry point is `PolicyEngine.decide(report, *, stage, category, draft_action)` (engine.py:167-174).

| Input | Type | Where it comes from |
|---|---|---|
| `report` | `GuardReport` (contracts/policy.py:88-113) with `message_id`, `organization_id`, `l1` (LayerVerdict), `l2` (SanitizedIntent), `l3` (LayerVerdict), `l3b` (list of ChunkVerdict), `l4` (OutputVerdict), and the output slots `inbound_decision`, `decision`, `total_latency_ms` | Filled by the pipeline as each layer finishes |
| `stage` | `"inbound"` or `"outbound"`, default `"outbound"` | The pipeline passes `"inbound"` after L1 and L2 (pipeline.py:151-154) and `"outbound"` after L4 (pipeline.py:256-262) |
| `category` | Triage category string or None | Caller argument, else the email's `category` field (pipeline.py:153). Outbound: caller, else draft, else email (pipeline.py:260) |
| `draft_action` | `reply`, `forward` or `escalate`, or None | The draft's `action` field (pipeline.py:261; contracts/email.py:142). None at the inbound gate |

L5 never reads the email text, the draft text or the chunk text. It reads only report-level values (engine.py:134-164). A layer that is switched off is None or empty in the report, and `verdicts()` skips None (contracts/policy.py:106-109).

**Facts derived from the report** (engine.py:134-164):

| Fact | Definition |
|---|---|
| `max_severity` | Highest severity over L1, L2, L3, L4 and every L3b chunk verdict (contracts/policy.py:111-113) |
| `threat_types` | Set of `threat_type` over every finding of every layer, whatever its severity (engine.py:139-143) |
| `layers_flagged` | Layers whose verdict severity is medium or higher (engine.py:144-145) |
| `layer_error` | True if any verdict has a non-empty `error` (engine.py:154) |
| `quarantine_ratio` | Quarantined L3b chunks divided by chunks scanned; 0.0 if none (engine.py:146-148) |
| `removed_ratio` | L2's characters stripped / (stripped + kept body characters); 0.0 without L2 (engine.py:156; contracts/verdict.py:163-167) |
| `complied_with_injected_goal`, `citation_mismatch` | L4 flags (engine.py:157-160) |
| `redactions` | Count of L4 redactions (engine.py:161) |
| `stage`, `category`, `draft_action` | Passed through unchanged |

## 3. How it decides

L5 has no model, no ML classifier, no LLM call and no score formula. The only thresholds are the ones in this section, plus a hard-coded 0.6 in step 5.

```
GuardReport ──▶ facts() ──▶ first rule (lowest priority number) whose `when` fully matches
                                   │ none matches -> default_action (draft_only)
                                   ▼
                     auto_send guard: not in auto_send_categories -> draft_only
                                   ▼
             risk tier (from max severity, bumped to T3 if quarantine ratio >= 0.6)
                                   ▼
                     PolicyDecision (+ one JSONL audit line if enabled)
```

**Stages, in order** (engine.py:175-239):

1. Compute the facts.
2. Walk the rules sorted by ascending `priority` (engine.py:131). The first rule whose every `when` condition holds wins (engine.py:176). All conditions inside one `when` are ANDed.
3. If no rule matches, use `default_action` (YAML: `draft_only`, policy.yaml:20; code default also `draft_only`, engine.py:108). The rule id is `"default"` and `requires_human` is true when the action rank is 2 or more (engine.py:177-183).
4. Safety catch. If the action is `auto_send` and the category is not in `auto_send_categories` (YAML: `acknowledgement`, `scheduling`; policy.yaml:21), the action is demoted to `draft_only` (engine.py:191-196). The matched rule id and `requires_human` stay as the rule set them. A missing category counts as not allowed. The comparison is exact and case-sensitive (engine.py:83-86, 192).
5. Risk tier. `RiskTier.from_severity(max_severity)` maps none to T0_CLEAN, low to T1_LOW, medium to T2_MEDIUM, high to T3_HIGH, critical to T4_CRITICAL (contracts/policy.py:31-38). If `quarantine_ratio >= 0.6` and the tier is below T3, it is raised to T3_HIGH (engine.py:198-199). This 0.6 is hard-coded in Python and separate from the 0.6 in rule P04. With the shipped L3b the bump is effectively dead code: a quarantined chunk always has severity high or above (see below), so the tier is already T3 or T4 whenever the ratio is above 0. It matters only for hand-built verdicts, such as the unit test (tests/unit/test_l5_policy.py:76-92), which uses medium quarantined chunks.
6. Build the reasons, an idempotent `audit_id`, and the decision object; if `audit=True`, append an audit line (engine.py:200-238).

**Where severity comes from (set by earlier layers, not by L5).** `Severity.from_score` (contracts/verdict.py:47-57) maps a score to a severity: score 0.97 or more is critical, at or above the `block` value is high, at or above the `flag` value is medium, 0.20 or more is low, anything else is none. The defaults are block 0.85 and flag 0.50. The layers differ:

- L1 passes its configured `l1.flag_threshold` 0.50 and `l1.block_threshold` 0.85 (config/settings.py:35-36; l1_injection_scanner/scanner.py:95, 158, 204).
- L2, L3 and L4 use the defaults. L4's heuristic findings score at most 0.95 (unsafe_action 0.9, injected goal 0.95, system-prompt leak 0.9), so they are never critical. An L4 LLM finding can be.
- L3b calls `from_score(fused, flag=0.5, block=quarantine_threshold)` with `l3b.quarantine_threshold` default 0.70 (l3b_document_scanner/scanner.py:308-309, 360-361; config/settings.py:68). A chunk is therefore quarantined exactly when its severity is high or critical. In the fail-closed error path a chunk is also quarantined with high severity (scanner.py:221-229).

**Condition keys the engine understands** (engine.py:45-88): `stage`, `max_severity_at_least`, `max_severity_at_most`, `threat_types_any`, `layers_flagged_any`, `layer_error_any`, `quarantine_ratio_at_least`, `removed_ratio_at_least`, `complied_with_injected_goal`, `citation_mismatch`, `redactions_at_least`, `category_in`, `action_in`. An unknown key is silently ignored, so it never makes a rule fail to match. `layers_flagged_any` and `redactions_at_least` are implemented but no shipped rule uses them (policy.yaml:23-108).

**Rules, in priority order.** All are in `configs/policy.yaml`, policy version `2026.09-v1` (policy.yaml:19). The file is chosen by `l5.policy_path` (default `configs/policy.yaml`, config/settings.py:84).

| Prio | Rule id | Condition | Action | requires_human | Targets |
|---|---|---|---|---|---|
| 0 | P00-internal-error-fail-closed | any layer has `error` | human_approval | true | a guard layer crashed (policy.yaml:24-29) |
| 10 | P01-critical-injection-quarantine | max severity >= critical | quarantine | true | near-certain injection or secret exposure (policy.yaml:31-36) |
| 20 | P02-exfiltration-or-tool-abuse-block | max severity >= high AND any finding of type data_exfiltration, tool_abuse, secret_leak or system_prompt_leak | block | true | data theft, tool abuse, leaks (policy.yaml:38-45) |
| 25 | P03-injected-goal-compliance-block | stage = outbound AND L4 `complied_with_injected_goal` | block | true | the draft carries the attacker's payload (policy.yaml:47-52) |
| 30 | P04-rag-poisoning-escalate | quarantine ratio >= 0.6 (no stage condition) | human_approval | true | most retrieved knowledge quarantined (policy.yaml:54-59) |
| 40 | P05-high-severity-human-approval | max severity >= high | human_approval | true | any other high finding (policy.yaml:61-66) |
| 45 | P06-outbound-unsafe-action-block | stage = outbound AND any finding of type unsafe_action | block | true | forward or outside recipient (policy.yaml:68-73) |
| 50 | P07-citation-mismatch-review | stage = outbound AND L4 `citation_mismatch` | human_approval | true | draft cites unretrieved knowledge (policy.yaml:75-80) |
| 60 | P08-medium-severity-draft-only | max severity >= medium | draft_only | false | medium signals (policy.yaml:82-87) |
| 65 | P09-heavy-sanitization-draft-only | L2 removed ratio >= 0.35 | draft_only | false | a large part of the email was stripped (policy.yaml:89-94) |
| 90 | P10-clean-auto-send | stage = outbound AND max severity <= low AND category in [acknowledgement, scheduling] AND draft action = reply | auto_send | false | the only path to auto-send (policy.yaml:96-101) |
| 1000 | P99-default | always | draft_only | false | default posture (policy.yaml:103-108) |

Rule defaults when a field is missing: `action` draft_only, `priority` 100, `requires_human` true when the action rank is 2 or more (engine.py:39-42). Action ranks: auto_send 0, draft_only 1, human_approval 2, block 3, quarantine 4 (contracts/policy.py:63-69).

**Order is by priority number, not by strictness.** `PolicyAction.strictest` exists (contracts/policy.py:59-60) but nothing in `mailguard/` or `services/` calls it; only tests/unit/test_contracts.py:19-20 does. Consequences, all read from the YAML order:

- P00 (human_approval) comes before P01 (quarantine). A layer error plus a critical finding in another layer yields `human_approval`, the weaker action.
- P05 (any high finding, human_approval, priority 40) comes before P06 (unsafe_action, block, priority 45). L4's heuristic unsafe-action finding is always high (scanner.py:392-399), so P05 always pre-empts P06 for it. With `l4.llm_enabled` off, as in the v1 benchmark (guard_factory.py:47-48), P06 cannot fire. It could fire only for an unsafe_action finding below high, which only L4's LLM path can create (scanner.py:494-511). Any unrecognized violation label is mapped to unsafe_action there (scanner.py:504), and no other layer emits that type.
- P04 and P05 have the same action. With the shipped L3b, any quarantined chunk already makes max severity high or above, so P04 differs from P05 only in the recorded rule id and reason.
- At the inbound stage, P00, P01, P02, P05, P08, P09 and P99 can fire. P03, P06, P07 and P10 are outbound-only by their `stage` condition. P04 has no stage condition but cannot fire inbound, because `report.l3b` is filled only later, in `build_prompt` (pipeline.py:191-192).

## 4. Output

The result is a `PolicyDecision` (contracts/policy.py:72-85). The pipeline stores it in `report.inbound_decision` and `report.decision` at the inbound gate (pipeline.py:152-155), and in `report.decision` only at the outbound gate (pipeline.py:257-262). `report.inbound_decision` is kept.

| Field | Type | Meaning |
|---|---|---|
| `audit_id` | str | First 32 hex characters of SHA-256 over `message_id`, `organization_id`, `stage`, policy version, action, rule id and max severity, joined by a vertical bar (engine.py:206-218). The same report and stage give the same id. It excludes category, draft action and content, so different reports with the same seed fields collide. |
| `action` | PolicyAction | `auto_send`, `draft_only`, `human_approval`, `block`, `quarantine` (contracts/policy.py:45-52) |
| `risk_tier` | RiskTier | `t0_clean` to `t4_critical` (contracts/policy.py:21-28) |
| `matched_rule_id` | str | Winning rule id, or `"default"` |
| `reasons` | list[str] | The rule's reason text, then one line `"<layer>: <severity> (score=x.xx, by=<decided_by>)"` for each verdict at medium or higher or with an error; capped at 12 (engine.py:200-205, 222) |
| `requires_human` | bool | From the rule (or the default posture) |
| `redactions_applied` | int | Number of L4 redactions (engine.py:224) |
| `quarantined_chunk_ids` | list[str] | Ids of L3b chunks with `quarantined=True` (engine.py:225) |
| `policy_version` | str | `version` from the YAML; `"unknown"` if the file is missing (engine.py:107, 122) |
| `decided_at` | datetime | Wall-clock time of the decision (contracts/policy.py:84); not part of `audit_id` |
| `metadata` | dict | `stage`, `category`, `draft_action`, sorted `threat_types`, sorted `layers_flagged`, and `quarantine_ratio` and `removed_ratio` rounded to 3 places (engine.py:227-235) |

**Severities and threat types.** L5 emits no finding, so it emits no severity or threat type of its own. It reads the severities none, low, medium, high and critical (contracts/verdict.py:33-40) and can act on any of the 17 `ThreatType` values (contracts/verdict.py:73-96). The shipped rules name only data_exfiltration, tool_abuse, secret_leak, system_prompt_leak and unsafe_action.

**Audit log** (only when `audit=True`). One JSON line per decision is appended to `l5.audit_log_path` (default `logs/mailguard_audit.jsonl`, config/settings.py:85). It holds ids, action, tier, rule, policy version, per-layer severity, score and decided_by (L3b chunk verdicts are excluded from that map), chunk counts and quarantined ids, threat types and the redaction count. It holds no email content (engine.py:242-273). The file is opened in append mode with no check for an existing `audit_id`, so a replayed job writes a second line with the same id. The v1 benchmark builds the pipeline with `audit=False`, so it writes no audit log (dazzling-bose/evaluation/mailguard_bench/guard_factory.py:52-56).

## 5. Hands on to later layers

L5 is the last layer, so nothing runs after it inside the guard.

```
inbound:   L1 ─▶ L2 ─▶ L5(inbound) ── block/quarantine ──▶ STOP (no L3b, L3, model call, L4)
                                  └── anything else ────▶ L3b ─▶ L3 ─▶ generate ─▶ L4 ─▶ L5(outbound)
final report.decision ─▶ caller / dispatcher (draft withheld on block/quarantine; sent only on auto_send)
```

- The outbound decision re-reads the same L1 and L2 verdicts plus L3, L3b and L4, so outbound is a cumulative re-evaluation, not a fresh look (pipeline.py:256-262; contracts/policy.py:106-109).
- Ablation presets (experiment configurations that switch layers on and off): C1 is L1 plus L5, C2 is L1+L2+L3+L5, C3 is all layers, and `C3-L5` turns L5 off (pipeline.py:56-66). With L5 off, `report.decision` stays None and `blocked(None)` is False, so nothing ever stops (pipeline.py:158-163). `decision_to_job_result` then reports `action=draft_only` by default (adapters.py:134).

## 6. Who acts on it

- **L5 itself blocks nothing and sends nothing.** It returns a decision object.
- **The pipeline acts on it in exactly one place.** `MailGuardPipeline.run` returns early, before L3b, L3 and generation, when the inbound decision is `block` or `quarantine` (`blocked()`, pipeline.py:158-163, 289-290). This is controlled by the `stop_on_inbound_block` argument (default True, pipeline.py:280, 289). `human_approval`, `draft_only` and `auto_send` do not stop the pipeline. The guard worker's outbound-only path (a job that already carries a `draft`) calls `inspect_inbound` and `build_prompt` directly and does not stop on an inbound block (services/guard_worker/main.py:36-45).
- **After the outbound gate the pipeline does not stop either.** `run` still returns the (L4-redacted) draft even when the final action is block or quarantine (pipeline.py:313-323), so the caller must withhold it. `decision_to_job_result` sets `draft` to None for block and quarantine (mailguard/integration/adapters.py:147-149). `dispatch_allowed` returns true only when the action is `auto_send`, `requires_human` is false and there are requested recipients (adapters.py:164-167). The README states that the dispatcher must send only on `auto_send`, that `draft_only` and `human_approval` create a reviewer draft, and that `block` and `quarantine` create nothing (README.md:113-114). The guard worker documents that it never sends mail itself (services/guard_worker/main.py:11-13). The dispatcher is not in this repository (not determined here).
- **In the rag-email benchmark**, `blocked_inbound` is true only for an inbound block or quarantine, and `blocked_outbound` only when the final decision is a different object from the inbound one and is block or quarantine (dazzling-bose/evaluation/mailguard_bench/guarded_reply.py:79-81). `final_draft` is None when blocked or when there is no draft (guarded_reply.py:119-123). Part C scoring treats `human_approval` (and `draft_only`) as not blocked: `final_fields` is None only if `record.blocked` (scoring.py:262), and benign utility requires `not record.blocked` (scoring.py:277-281). So an attack goal achieved in a `human_approval` draft counts as achieved, and a benign `human_approval` reply counts as a success. The action and rule are only recorded (scoring.py:299-301).

## 7. Failure behavior

- **Exception inside `decide`.** Not caught: `decide` has no try/except. An exception in the facts, in rule matching, or in decision building propagates to the caller of `inspect_inbound` or `inspect_outbound` (engine.py:167-239; pipeline.py:152, 257). An example is an invalid severity name in a rule value, which makes `_severity` raise ValueError at match time (engine.py:32-33, 51). The guard worker catches any exception per job (stdin mode: `run_stdin`; AMQP mode: the loop in `run_amqp`) and emits `{"error": "<Type>: <msg>"}` with no `mailguard` decision block (services/guard_worker/main.py:86-89 and the AMQP except). What a dispatcher does with such an error message is not determined here.
- **Policy file missing.** L5 logs a warning, sets `rules = []` and keeps `default_action = draft_only`. Every decision is then rule `"default"` with `draft_only`, and `auto_send_categories` stays empty (engine.py:113-119, 108-109). A missing file therefore never blocks anything, even a critical finding. `version` stays `"unknown"`.
- **Invalid rule in the YAML** (missing `id`, or an unknown action string). Only `KeyError` and `ValueError` are caught; that rule is skipped with a logged error and the others load (engine.py:125-131). A dropped security rule silently lets the decision fall through to a weaker rule or the default. A rule entry that is not a mapping (or a `when` that is not a mapping) raises `AttributeError` or `TypeError`, and the error handler itself calls `raw.get`, so this propagates from the constructor. An invalid top-level `default_action` raises ValueError from the constructor (engine.py:123).
- **Malformed YAML.** `yaml.safe_load` raises in the constructor; there is no handler (engine.py:120-121).
- **Audit log unwritable.** An `OSError` is logged and the decision is still returned (engine.py:274-275). Other exception types from the audit path are not caught.
- **A guard layer crashed upstream.** Layers convert exceptions into a verdict with `error` set. When `mailguard.fail_closed` is true (the default), that verdict has severity high, score 0.9 and an `internal_error` finding (layers/base.py:41-62; config/settings.py:94-98). With `fail_closed=false` the severity is none and the score 0.0, but `error` is still set, so P00 still fires. L5 rule P00 then yields `human_approval` (policy.yaml:24-29; engine.py:154). This is "fail-closed to a reviewer", not "fail-closed to block": the pipeline continues to generation.
- **LLM timeout, non-JSON or schema-invalid output in another layer.** L5 has no LLM of its own. The layers that do have one catch `LLMError`, keep their cheap (rule or ML) verdict, and write only `metadata["llm_error"]`, leaving `error` empty (L1: l1_injection_scanner/scanner.py:180-183; L2: l2_intent_extractor/extractor.py:334-337; L3b: l3b_document_scanner/scanner.py:335-337; L4: l4_output_scanner/scanner.py:487-490). L5 tests only `error` (engine.py:154), so it does not see these degradations and P00 does not fire. The rag-email benchmark reads `llm_error` itself and turns it into an error row (dazzling-bose/evaluation/mailguard_bench/guarded_reply.py:198-225). An LLM exception that is not `LLMError` propagates to the layer's own handler, which produces the fail-closed error verdict above (l1 scanner.py:135-137).

## 8. Cost and speed

- No LLM call, no model inference and no network. Work per decision is one pass over about 12 rules against a small dict of facts, one SHA-256, and (optionally) one file append (engine.py:167-239, 242-273). The YAML is read once at construction, not per decision (engine.py:111).
- Latency: the README states "< 1 ms" (README.md:29). That is the authors' figure; it was not measured here, and no timer exists in L5 code. L5 is not included in `report.total_latency_ms`, which sums layer verdicts only (pipeline.py:150, 207, 255), so the repository records no L5 latency (not determined).
- Model calls attributable to L5: zero, in v1 and in any configuration.

## 9. Blind spots

These are by construction, read from the code.

1. **It sees only what other layers reported.** It reads no email, draft or chunk text (engine.py:134-164). If L1, L2, L3b and L4 all miss an attack, L5 outputs a clean-looking decision. A clean report with category `acknowledgement` or `scheduling` and action `reply` is auto-sent (policy.yaml:96-101).
2. **Only two actions stop anything, and the inbound gate stops only before generation.** A high-severity inbound finding whose threat type is not data_exfiltration, tool_abuse, secret_leak or system_prompt_leak (for example plain `prompt_injection` or `instruction_override`) gets P05 `human_approval`, and the pipeline still builds the prompt and calls the model on that email (policy.yaml:38-45, 61-66; pipeline.py:158-163, 289-303). Only critical severity (P01) or a listed threat type at high (P02) stops it.
3. **A weaker action when an error coincides with a critical finding** (P00 before P01, section 3).
4. **Threat type and severity are not linked.** P02 needs max severity of high or more and any finding of a listed type, but the listed-type finding may itself be low, and the high severity may come from a different finding (engine.py:139-143, 59-62).
5. **Single-layer maximum, no corroboration or weighting.** One medium verdict and five medium verdicts both produce `draft_only` (P08). L5 only takes the maximum severity (contracts/policy.py:111-113).
6. **Neutralized chunks still count, and can escalate past human_approval.** `max_severity` and `threat_types` include every L3b chunk verdict, including chunks L3b already removed from the prompt (contracts/policy.py:106-113; pipeline.py:183-192). A quarantined chunk is by construction high or critical (l3b scanner.py:308-309). At the outbound gate, one removed chunk therefore forces at least P05 `human_approval`. It forces P01 `quarantine` if its score is 0.97 or more, or P02 `block` if any of its findings has a listed threat type (L3b runs the same rule engine as L1), even though the poison never reached the model. In the benchmark such a case counts as blocked_outbound. The inbound gate is unaffected, because chunks are scanned after it. The reverse direction is only partly invisible: a kept chunk with a fused score of 0.50 to 0.70 is medium and reaches L5 (P08 `draft_only`), one at 0.20 to 0.50 is low, and only chunks below 0.20 are effectively unseen.
7. **Layer LLM failures are invisible** (section 7).
8. **`human_approval` and `draft_only` are only labels here.** Their effect depends on a reviewer or dispatcher outside this repository (README.md:113-114). In the rag-email benchmark they are scored as delivered drafts (section 6).
9. **Rule authoring errors are silent.** Unknown `when` keys are ignored, so a misspelled key makes a rule match more broadly (engine.py:45-88). An invalid rule is dropped (engine.py:126-130). A missing file downgrades every decision to `draft_only` (engine.py:113-119).
10. **Duplicate thresholds.** The 0.6 quarantine ratio exists in P04 (policy.yaml:54-59), hard-coded in engine.py:198, and as `l3b.max_quarantine_ratio` (config/settings.py:70-72). L5 does not read the setting; a grep finds it only in settings.py and a docstring in the L3b scanner (scanner.py:14), and `RetrievedDocumentScanner.quarantine_ratio` (scanner.py:274) is not called by the pipeline. The chunk-level threshold that actually matters is `l3b.quarantine_threshold` 0.70. With the shipped L3b, the tier bump and P04 add nothing beyond P05 (section 3).
11. **`auto_send` depends on the category label** supplied by the caller, matched exactly and case-sensitively. If the triage category is wrong, differently cased or absent, the guard cannot correct it (engine.py:83-86, 191-196). Whether benchmark cases carry `acknowledgement` or `scheduling` categories is not determined; the benchmark lower-cases the case category and defaults an empty one to `general_inquiry` (dazzling-bose/evaluation/mailguard_bench/case_adapter.py:50, 158).

## 10. Why it is needed even with the other layers

The other layers each produce a score or a filtered artifact (L2 a sanitized body, L3b a kept-chunk list, L4 a redacted draft). None of them decides what happens to the message. In this code, the only thing that stops the pipeline is L5's decision: with L5 off, `report.decision` is None and `blocked()` is False (pipeline.py:158-163, 289-290). L1 alone would give a severity and nothing that gates generation or dispatch (C1 as defined includes L5, pipeline.py:56-58).

What L5 covers that L1 cannot, argued from the code:

- **Signals L1 never sees.** L1 reads the inbound email only (`inspect(email)`, l1_injection_scanner/scanner.py:123). L5 also acts on the L3b quarantine ratio (RAG poisoning, where the email itself can be clean; P04), L4 injected-goal compliance (the model obeyed an injection that L1 missed; P03), L4 citation mismatch (P07), L4 system-prompt and secret leaks (P02), and unsafe forwards (P05, P06). Those facts exist only after retrieval and generation (pipeline.py:191-192, 244-254).
- **Failure handling.** A crashed layer becomes a mandatory reviewer through P00, instead of being silently treated as clean (policy.yaml:24-29; layers/base.py:41-62).
- **Graduated response instead of one threshold.** L1 has one flag threshold (0.50) and one block threshold (0.85) (config/settings.py:35-36). L5 adds tiers: quarantine for critical, block for exfiltration types, human approval for other high findings, and draft-only for medium, plus a heavy-stripping rule from L2 (P09). It also holds the single rule that allows auto-send and forces everything else to a draft (policy.yaml:96-108; engine.py:191-196).
- **Consistency and traceability.** The same report and stage give the same `audit_id`, so a consumer can deduplicate replays; the engine itself does not, and the audit log appends a duplicate line. Every decision records the winning rule and reasons (engine.py:206-218, 200-205).
- **Honest limit.** L5 adds no detection ability. It cannot catch an attack that no layer flagged, and section 9 lists cases where its ordering or enforcement is weaker than the label suggests (and one, blind spot 6, where it is stricter than the model's actual exposure). Its value is coordination and gating, not detection.

## 11. What it did in the v1 benchmark

Runs `2026-09-29-gpt4omini` (gpt-4o-mini) and `2026-09-29-qwen25` (qwen2.5:7b-instruct), configuration C3 (L1, L2, L3, L3b, L4, L5). Each run has 550 ok rows and no error rows: 300 LLMail email attacks, 100 RAG attacks (89 poisonedrag and 11 seed_rag), and 150 LLMail benign emails. The L3b and L4 LLM stages were off in both runs, so those layers were heuristic and ML only. Single run per model. Llama-3.1-8B is left out of this card; a prompt-wiring gap in that run has been noted separately and was not verified here.

**Blocks (block or quarantine).** gpt-4o-mini blocked 317 of 400 attacks (285 inbound, 2 email outbound, 30 RAG outbound). qwen2.5 blocked 316 (286 inbound, 3 email outbound, 27 RAG outbound).

| Blocks by stage and rule | gpt-4o-mini | qwen2.5 |
|---|---|---|
| Inbound P01 quarantine | 281 | 282 |
| Inbound P02 block | 4 | 4 |
| Outbound P03 block | 32 (2 email, 30 RAG) | 30 (3 email, 27 RAG) |
| Total | 317 | 316 |

No P04, P05, P07 or P08 outcome was a block; those rules only escalate to `human_approval` or `draft_only`.

**Rule counts at the inbound gate (evaluated on every row).**

| Group | gpt-4o-mini | qwen2.5 |
|---|---|---|
| Attack email (300) | P01 281, P05 11, P02 4, P08 4 | P01 282, P05 10, P02 4, P08 3, P99 1 |
| Attack RAG (100) | P05 86, P99 11, P08 3 | P99 100 |
| Benign (150) | P99 147, P08 3 | P99 147, P08 3 |

The 86 gpt P05 outcomes on RAG are an L2 artifact, not detection of poisoned knowledge. gpt's L2 flagged 89 of 100 RAG emails as prompt injection because all 89 poisonedrag emails share the wrapper "Hi, a quick factual question for your knowledge assistant". qwen's L2 flagged none.

**Outbound gate, final decision, only rows that reached generation.**

| Group | gpt-4o-mini | qwen2.5 |
|---|---|---|
| Attack email (15 gpt, 14 qwen) | P05 9, P08 4, P03 2 | P05 8, P03 3, P08 3 |
| Attack RAG (100) | P05 53, P03 30, P04 13, P08 3, P99 1 | P03 27, P99 24, P07 16, P08 14, P05 10, P04 9 |
| Benign (150) | P99 147, P08 3 | P99 147, P08 3 |

**Attack outcome.** Under C3, no LLMail attack achieved its goal (0 of 300, both models), against 162 (gpt-4o-mini) and 155 (qwen2.5) of about 300 in the unguarded C0 run. C0 and C3 use separate generation samples. On RAG, the final C3 output still achieved the attacker goal in 32 (gpt-4o-mini) and 39 (qwen2.5) of 100 cases, against 79 and 91 in C0.

**RAG blocks are a weak proxy for attack success.** All RAG blocks are P03. Only 7 of 30 (gpt-4o-mini) and 5 of 27 (qwen2.5) blocked drafts had achieved the attacker goal before L4. L4's compliance check is a 6-gram overlap between the draft and the "injected instructions", and those inputs include quarantined-chunk excerpts and, for gpt-4o-mini, L2's own extraction of the customer's question wrapper. Meanwhile 33 (gpt-4o-mini) and 42 (qwen2.5) of the unblocked RAG cases had a goal-achieving draft before L4 (the final-output counts of 32 and 39 above differ from these; the gap was not traced).

**Benign emails.** No layer blocked any of the 150, so the 0 percent false-block rate is a real property of these runs. Three emails ("updated guidelines" emails where L2 strips legitimate instructions) reached L2 medium and were routed to P08 `draft_only`, which is not a block. The benign set is LLMail-FP emails that AgentMailGuard also used as L1 negatives, so L1's benign performance is likely optimistic. There are no benign RAG rows in this benchmark.

**Offline counterfactual (simulation, not a run).** The evidence replayed AgentMailGuard's own `PolicyEngine` (policy `2026.09-v1`, audit off) on the stored L1 and L2 verdicts, with each layer removed in turn. The replay reproduced the stored inbound decision on 550 of 550 rows per model, including `audit_id`. It covers the inbound gate only and cannot say what L3, L3b or L4 would have done to attacks that stop being blocked.

| Inbound blocks on the 300 LLMail attacks | gpt-4o-mini | qwen2.5 |
|---|---|---|
| Full (L1 and L2) | 285 | 286 |
| L2 alone | 267 (93.7 %) | 268 (93.7 %) |
| L1 alone | 248 (87.0 %) | 250 (87.4 %) |
| Neither | 0 | 0 |
| Blocks lost without L1 | 18 | 18 |
| Blocks lost without L2 | 37 | 36 |

Without L1, the lost blocks fall mostly to P05 `human_approval`, not to auto-send. In the unguarded C0 run, 6 of the 18 (gpt-4o-mini) and 5 of the 18 (qwen2.5) would have achieved the attacker goal. The real C1 run (L1 plus L5) blocked 86 of the 100-attack subset in both models, matching the simulated L1-alone value of 86.

## 12. Worked example

Traced from the code, not run. The upstream verdicts are assumed inputs, shaped like the ones in tests/unit/test_l5_policy.py:20-28 and 63-73.

**Example A: inbound gate.** Assumed report: `GuardReport(message_id="m", organization_id="o", l1=LayerVerdict(layer=l1_injection_scanner, severity=high, score=0.93, findings=[Finding(threat_type=data_exfiltration, severity=high, score=0.93, detector="rule")]))`, called with `stage="inbound"` and `category="support"`.

- Facts: `max_severity=high`; `threat_types={"data_exfiltration"}`; `layers_flagged={"l1_injection_scanner"}`; `layer_error=False`; `quarantine_ratio=0.0`; `removed_ratio=0.0` (no L2); `complied=False`; `citation_mismatch=False`; `redactions=0`.
- Rule walk: P00 has no error, no; P01 needs critical, no; P02 has severity high and a listed threat type, yes, so the walk stops.
- Output: `action=block`, `matched_rule_id="P02-exfiltration-or-tool-abuse-block"`, `risk_tier=t3_high`, `requires_human=True`, `redactions_applied=0`, `quarantined_chunk_ids=[]`, `policy_version="2026.09-v1"`, `reasons=["High-severity exfiltration / tool abuse / secret or system-prompt leak.", "l1_injection_scanner: high (score=0.93, by=rule)"]`, `metadata={"stage":"inbound","category":"support","draft_action":None,"threat_types":["data_exfiltration"],"layers_flagged":["l1_injection_scanner"],"quarantine_ratio":0.0,"removed_ratio":0.0}`. The `audit_id` is a 32-hex SHA-256 prefix (value not computed by hand).
- Pipeline effect: `blocked()` is true, so `run` returns `(report, None, None)`. There is no L3b, no L3 and no model call.

**Example B: outbound gate, showing rule order.** Assumed report: L1 clean, `l4=OutputVerdict(severity=high, score=0.9, findings=[unsafe_action, high])` (what L4's heuristic emits when the draft action is `forward`, scanner.py:392-399), `stage="outbound"`, `category="support"`, `draft_action="forward"`.

- Facts: `max_severity=high`, `threat_types={"unsafe_action"}`, `layers_flagged={"l4_output_scanner"}`, no error, `complied=False`.
- Rule walk: P00 no; P01 no; P02 has severity high but `unsafe_action` is not in its list, no; P03 needs compliance, no; P04 ratio 0.0, no; P05 high, yes, so the walk stops. P06 is never reached.
- Output: `action=human_approval`, `matched_rule_id="P05-high-severity-human-approval"`, `risk_tier=t3_high`, `requires_human=True`, `reasons=["High-severity finding in at least one layer.", "l4_output_scanner: high (score=0.90, by=rule)"]`.
- Pipeline effect: not blocked, so the draft is returned. In the benchmark record, `blocked=False` and `final_draft` is kept (guarded_reply.py:79-81, 119-123), and Part C scores it as a delivered draft (scoring.py:262).

## 13. Sources

Paths are relative to the AgentMailGuard-bench worktree at commit `81df5d07` unless they start with `dazzling-bose/`.

| File | Lines used |
|---|---|
| mailguard/layers/l5_policy_engine/engine.py | 32-33, 39-42, 45-88, 107-131, 134-164, 167-239, 242-275 |
| configs/policy.yaml | 19-21, 23-108 |
| mailguard/contracts/policy.py | 21-38, 45-69, 72-85, 88-113 |
| mailguard/contracts/verdict.py | 33-40, 47-57, 73-96, 163-167 |
| mailguard/contracts/email.py | 142 |
| mailguard/pipeline.py | 56-66, 150-163, 183-192, 207, 244-262, 280, 289-323 |
| mailguard/config/settings.py | 35-36, 68-72, 84-85, 94-98 |
| mailguard/layers/base.py | 41-62 |
| mailguard/layers/l1_injection_scanner/scanner.py | 95, 123, 135-137, 158, 180-183, 204 |
| mailguard/layers/l2_intent_extractor/extractor.py | 334-337 |
| mailguard/layers/l3b_document_scanner/scanner.py | 14, 221-229, 274, 308-309, 335-337, 360-361 |
| mailguard/layers/l4_output_scanner/scanner.py | 392-399, 487-511 |
| mailguard/integration/adapters.py | 134, 147-149, 164-167 |
| services/guard_worker/main.py | 11-13, 36-45, 86-89 |
| README.md | 29, 113-114 |
| tests/unit/test_l5_policy.py | 20-28, 63-92 |
| tests/unit/test_contracts.py | 19-20 |
| dazzling-bose/evaluation/mailguard_bench/guard_factory.py | 47-56 |
| dazzling-bose/evaluation/mailguard_bench/guarded_reply.py | 79-81, 119-123, 198-225 |
| dazzling-bose/evaluation/mailguard_bench/scoring.py | 262, 277-281, 299-301 |
| dazzling-bose/evaluation/mailguard_bench/case_adapter.py | 50, 158 |
| v1 evidence, runs `2026-09-29-gpt4omini` and `2026-09-29-qwen25`, config C3 (also C0, C1, C2 for cross-checks) | raw rows and the offline replay described in section 11 |
