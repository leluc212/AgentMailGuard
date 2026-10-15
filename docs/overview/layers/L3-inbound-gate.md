# L3 - Inbound Gate

Reference card for layer 3 of AgentMailGuard (`mailguard/layers/inbound_gate/gate.py`), written for the v3 guard (ADR-0015). It replaces the v2 card "L5 Policy Engine". In v3 the policy engine is **shared infrastructure** (`mailguard/policy/engine.py`), not a layer: L3 is the gate that runs it at stage `inbound` before any reply model call, and L4 Output Guard runs it at stage `outbound`. There is no separate "policy layer" and no outbound gate. No v3 benchmark number exists yet (plan_v3 Task 16).

Terms: a **verdict** is the record one layer returns (a severity, a score and findings); a **severity** is none, low, medium, high or critical; a **threat type** is a label on a finding such as `data_exfiltration`; **fail-closed** means choosing the cautious outcome on an internal error; **facts** are the summary values the engine computes from the layers' verdicts; a **gate pass** is one evaluation of the inbound policy.

## Data flow

```
email -> L1 Inbound Guard -> L3 gate pass 1 (L1 facts only; documents "not scanned")
                                  |  block or quarantine: STOP here (no Doc Scanner, no doc judge call,
                                  |  no model call); the host files an escalated holding draft
                                  v  otherwise
            L2 Doc Scanner (only if there are documents) -> L3 gate pass 2 (every fact; final, audited)
                                  |  block or quarantine: STOP (escalated draft, no model call)
                                  v  otherwise
            channel isolation -> one reply generation -> L4 Output Guard
                                  (outbound policy + the inbound decision carried in as a floor)
```

## 1. Purpose

L3 turns the inbound layers' verdicts (L1 and, in pass 2, L2) into one action (`quarantine`, `block`, `human_approval`, `draft_only` or `auto_send`) before the reply model is called, using the ordered YAML rules of the shared policy engine. It detects nothing itself. v3 moved it ahead of generation **and after the Doc Scanner**: in v2 the document scan ran after the inbound decision, so poisoned knowledge could never stop a job.

## 2. Input

`InboundGate.decide(report, category=..., gate_pass=1|2, final=bool)` calls `PolicyEngine.decide(report, stage="inbound", gate_pass=...)`.

- Pass 1 sees the report with only the Inbound Guard filled; the documents are not scanned yet, so the facts about documents (quarantine ratio, document findings) are empty and policy rules P01 and P04 cannot fire from them.
- Pass 2 sees every inbound fact. It runs only when pass 1 did not stop the job.
- `category` is the triage category (the caller's, else the email's). The gate never reads email, draft or chunk text: only report-level values.

**Facts derived from the report** (`PolicyEngine.facts`): `max_severity` (the highest severity over the layer verdicts present: Inbound Guard, Doc Scanner, Output Guard), `threat_types` (every finding's type, whatever its severity), `layers_flagged` (layers at medium or higher), `layer_error` (any verdict's `error`, including `judge_invalid:<stage>`, or an isolation error), `quarantine_ratio` (quarantined chunks / chunks scanned), `removed_ratio` (the Inbound Guard's stripped / total characters), `isolation_forged_markers`, `docs_scanned`, and, at stage outbound only, `complied_with_injected_goal`, `citation_mismatch`, `redactions`.

## 3. How it decides

No model, no score formula. The engine collects **every** rule whose `when` block fully matches; the **strictest** action wins (`quarantine > block > human_approval > draft_only > auto_send`), ties go to the lowest priority number; a rule with an empty `when` is the catch-all (P99) and applies only when nothing else matched. Then:

1. The auto-send guard: an `auto_send` outside `auto_send_categories` (`acknowledgement`, `scheduling`) becomes `draft_only`.
2. The risk tier comes from the max severity (T0 clean to T4 critical), raised to T3 when the quarantine ratio is 0.6 or more.
3. The decision gets reasons (the rule's reason plus one line per layer at medium or higher or with an error), an idempotent `audit_id` and, for a final pass, one audit-log line.

Policy `configs/policy.yaml`, version `2026.10-v3` (the rules are unchanged from `2026.09-v2`):

| Prio | Rule id | Condition | Action | At the inbound gate? |
|---|---|---|---|---|
| 0 | P00-internal-error-fail-closed | any layer has `error` | human_approval | yes |
| 10 | P01-critical-injection-quarantine | max severity critical | quarantine | yes |
| 20 | P02-exfiltration-or-tool-abuse-block | max severity >= high and a threat type in data_exfiltration, tool_abuse, secret_leak, system_prompt_leak | block | yes |
| 25 | P03-injected-goal-compliance-block | stage outbound and the draft complied with an injected goal | block | no (outbound) |
| 30 | P04-rag-poisoning-escalate | quarantine ratio >= 0.6 | human_approval | pass 2 only |
| 40 | P05-high-severity-human-approval | max severity >= high | human_approval | yes |
| 45 | P06-outbound-unsafe-action-block | stage outbound and an `unsafe_action` finding | block | no |
| 50 | P07-citation-mismatch-review | stage outbound and a citation mismatch | human_approval | no |
| 60 | P08-medium-severity-draft-only | max severity >= medium | draft_only | yes |
| 65 | P09-heavy-sanitization-draft-only | `removed_ratio` >= 0.35 | draft_only | yes |
| 90 | P10-clean-auto-send | stage outbound, severity <= low, category in the allowlist, draft action reply | auto_send | no |
| 99 | P99-default | always (catch-all) | draft_only | yes |

- Because the strictest match wins, P00 (a layer error, human_approval) no longer hides a stricter P01 or P02: the decision is the quarantine or block, with P00 among the matched rule ids.
- A pass 1 that returns block or quarantine **stops the job**: `report.doc_scanner` stays `None`, `metadata.gate_passes` = 1, and no document judge call is made. Otherwise the final decision is pass 2 (`gate_passes` = 2). A pass 1 that does not stop is provisional and is not audited; only the final pass is audited, and its `audit_id` seed includes the pass number so a replayed job gets the same id and the two paths never share one.
- With the gate switched off (`GuardConfig.inbound_gate` false: C3-L3) no inbound decision is taken and generation is not stopped inbound; the Output Guard still decides after generation.

## 4. Output

A `PolicyDecision`, stored as `report.inbound_decision` and `report.decision` (the final inbound decision; L4 later overwrites `report.decision` with the merged final decision and keeps `inbound_decision`).

| Field | Meaning |
|---|---|
| `action`, `risk_tier`, `requires_human` | the outcome |
| `matched_rule_id`, `matched_rule_ids` | the winner and every rule that matched |
| `reasons` | the rule's reason, then one line per flagged or failed layer (at most 12) |
| `quarantined_chunk_ids` | the Doc Scanner's quarantined chunks (pass 2) |
| `policy_version`, `audit_id`, `decided_at` | `2026.10-v3`; a deterministic id; the time |
| `metadata` | `stage`, `gate_passes`, `category`, sorted `threat_types` and `layers_flagged`, `quarantine_ratio`, `removed_ratio` |

The audit log (`POLICY__AUDIT_LOG_PATH`, default `logs/mailguard_audit.jsonl`) holds ids, action, tier, rule, policy version, per-layer severity, score and decided_by, chunk counts and quarantined ids, threat types and the redaction count, never email content.

## 5. Hands on to later layers

- A decision that is not block or quarantine lets the job continue: Doc Scanner (after pass 1), channel isolation, the reply call.
- A stop gives the host an **escalated holding draft** (`MailGuardPipeline.build_escalated_draft`: action `escalate`, a fixed body that never quotes the email, a subject naming the rule and reason; no model call). `MailGuardPipeline.run` itself returns `(report, None, None)` on such a stop.
- L4 Output Guard carries the inbound decision into its own as a floor (see `L4-output-guard.md`): a floor exists only when a specific (non-catch-all) rule produced the decision and its action is stricter than `auto_send`.

## 6. Who acts on it

Block and quarantine stop generation; `human_approval` and `draft_only` do not (the draft is produced and a reviewer sees it). The report's exit point in the benchmark names where a job ended: `gate_pass_1`, `gate_pass_2`, `output_guard` or `delivered`. The host (rag-email, via `decision_to_job_result`, which keeps every field and adds `reason`, `decided_by` and `guard_schema_version`) must send only when the final decision is `auto_send`; the host-side consumers of `trust` are ADR-0016 and are not built.

## 7. Failure behavior

- A failed or invalid judge, or an exception in a layer, sets that verdict's `error`; the `layer_error` fact then matches P00 and the gate asks for human review (and any stricter matching rule still wins). A judge that is not configured is not an error.
- A missing policy file gives the default posture (`draft_only`) and `policy_version` `unknown`.
- The audit write is best effort and never changes the decision.

## 8. Cost and speed

No model call and no network: a handful of comparisons per pass; two passes when documents are present. Not measured separately (the benchmark reports guard latency per configuration, Task 16).

## 9. Blind spots

1. It is only as good as the verdicts it reads: a miss in L1 or L2 reaches generation, and the gate cannot know.
2. Pass 1 cannot see documents, so a job that only the documents would stop is stopped at pass 2, after the Doc Scanner has run.
3. Severity and threat types are pooled over layers, so a low-severity finding of one layer can combine with a high verdict of another to fire P02.
4. An unknown key in a rule's `when` block is silently ignored (it never makes a rule fail to match), so a typo in `policy.yaml` weakens a rule.
5. The auto-send allowlist and the 0.6 risk-tier bump are in code or in YAML but are not tuned on measured data.
6. `audit_id` excludes category and content, so two different jobs with the same seed fields collide.

## 10. Why it is needed even with the other layers

Layers detect; the gate decides. Without it a HIGH or CRITICAL verdict would change nothing, because the layers return data and stop nothing themselves. It is also the only place the "stop before any model call" guarantee lives, and the only place where evidence from the email and from the retrieved documents is combined before generation.

## 11. v1/v2 results (pre-v3)

The earlier benchmark rows for the "L5 policy engine" (v1 and v2, six layers, an outbound gate) are history and not comparable: the rule set is the same but the gate now runs twice, after the Doc Scanner, and only L4 decides after generation. Re-read the committed folders with `make mailguard-report RUN=<id> LEGACY=1`.

## 12. Worked example (traced from the policy, no model run)

An email scanned by L1 as CRITICAL (rule `ignore-previous-instructions`, score 0.97), with three retrieved documents:

1. L1 returns severity critical. Gate pass 1 (L1 facts only): P01 matches (quarantine); P05 and P08 also match; the strictest wins: `quarantine`, `matched_rule_id = P01-...`, `matched_rule_ids` lists all matched. Pass 1 stops the job: `final=True` audited, `gate_passes = 1`, the Doc Scanner is not run, no model is called.
2. Same email scanned as MEDIUM (score 0.6) with two of three documents quarantined by the Doc Scanner (ratio 0.67): pass 1 gives P08 (draft_only) and does not stop; the Doc Scanner runs; pass 2 matches P04 (human_approval, ratio >= 0.6), P05 (a quarantined chunk is at least high) and P08; the strictest is `human_approval`, `gate_passes = 2`, and generation proceeds with the kept chunk and a reviewer is required. L4 later carries this decision as the floor.

## 13. Sources

Code (`agentmailguard/`, without line numbers): `mailguard/layers/inbound_gate/gate.py` (`InboundGate`), `mailguard/policy/engine.py` (`PolicyEngine`, facts, selection, audit), `configs/policy.yaml`, `mailguard/contracts/policy.py` (`GuardReport`, `PolicyDecision`), `mailguard/pipeline.py` (`_inspect_inbound`, `build_escalated_draft`), `mailguard/integration/adapters.py` (`decision_to_job_result`), `docs/adr/0015-guard-architecture-v3.md` (decisions 2 and 3).
