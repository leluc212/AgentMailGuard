# L4 - Output Guard

Reference card for layer 4 of AgentMailGuard (`mailguard/layers/output_guard/`), written for the v3 guard (ADR-0015). It replaces the v2 "L4 Output Scanner" **and** the v2 outbound policy step: in v3 the Output Guard is one layer that scans the draft, runs the shared policy engine at stage `outbound`, and merges in the inbound decision as a floor. There is no separate outbound gate. The scanner stages (sections 3 to 3b) are those of the v2 scanner, unchanged; the merge and floor are new. No v3 benchmark number exists yet (plan_v3 Task 16).

Terms used in this card:

- **Draft**: the reply text the language model wrote.
- **Redaction**: replacing a matched piece of text with a placeholder such as `[REDACTED:pii.email]`.
- **n-gram**: a run of n consecutive words.
- **Fail-closed**: when the layer itself breaks, treat the result as dangerous rather than harmless.
- **Indicator**: an email address or URL that appeared in the inbound email.
- **PII**: personally identifiable information (card numbers, phone numbers, email addresses and similar).
- **Floor**: the inbound gate's decision, carried into the final decision so that the strictest action wins across both gates.

Where the code is (under `agentmailguard/mailguard/`): `layers/output_guard/guard.py` (`OutputGuard`, `merge_floor`), `scanner.py` (`OutputScanner`), `policy/engine.py`, `contracts/verdict.py` (`OutputVerdict`), `config/settings.py` (`OutputGuardSettings`), `configs/pii_patterns.yaml`, `configs/policy.yaml`.

## Data flow

```
draft + inbound report (hand-offs) ---> output scanner (7 stages) ---> OutputVerdict (redacted draft)
                                                                              |
                                          shared PolicyEngine, stage "outbound"  <---+
                                                                              |
                          inbound decision (floor) ---------------------> merge_floor
                                                                              |
                                  final PolicyDecision  =  report.decision  (audited once)
```

## 1. Purpose

L4 reads the finished reply draft (after the model has written it) and checks whether it leaks secrets, personal data or hidden instructions, cites knowledge it was never shown, repeats an attacker's payload, forwards mail to outsiders, or contains unexpected links. It rewrites the draft to mask secrets and personal data, then decides the **final** action with the outbound policy and the inbound floor.

## 2. Input

`OutputGuard.inspect(report, draft, email=..., intent=..., kept_chunks=..., protected_texts=..., category=...)` is called by `MailGuardPipeline.inspect_outbound`, and only when the preset has L4 on (C3 and the ablations other than C3-L4). With L4 off (C0, C1, C2) `report.decision` stays the inbound decision.

| Argument | Source |
|---|---|
| `draft` | the generation step's `DraftCandidate` (body, citations, recipients, action `reply`/`forward`/`escalate`, subject). Only `draft.body` is scanned. |
| `email` | the inbound email (`full_text`, `sender_email`, `recipients`; `cc` is not used) |
| `intent` | the Inbound Guard verdict, used only by the optional LLM stage |
| `kept_chunks` | the chunks the Doc Scanner kept: their citation ids and texts are the allowed citations and trusted texts |
| `protected_texts` | `[system_instructions, category_instructions]` |
| `injected_indicators`, `injected_instructions` | from `injected_context(report)`: the Inbound Guard scan's address and URL indicators (every address and URL in the email, the sender's included); the scan judge's `injected_instructions`; the stripped segments' text and the extractor's `instructions_to_assistant`; the excerpts of findings of chunks the Doc Scanner quarantined; de-duplicated, at most 20 |
| `report.inbound_decision` | the floor candidate (None when the inbound gate is off) |
| `report.isolation` | read by the policy as the facts `isolation_forged_markers` (no default rule uses it) and a layer error |

## 3. How it decides: the output scanner

Everything except stage 7 is deterministic Python (regex, set and string operations). There is no model and no ML classifier in the default path. Stages 1 to 6 all run (no early exit); findings are collected, then merged.

```
draft + inbound email + Inbound Guard and Doc Scanner hand-offs
   |
   v
[1 patterns] [2 protected n-gram] [3 citations] [4 injected goal] [5 unsafe action] [6 external link]
   |  each adds Findings (a score per finding)
   v
redact (right to left)              score = max(finding scores) -> severity band
   \________________________ OutputVerdict ________________________/
                                    |
                                    |  (optional) stage 7: LLM judge, only if
                                    |  enabled AND severity < high AND (goals found OR score >= 0.2)
                                    v
                       report.output_guard  ->  policy engine (stage outbound) + inbound floor
```

**Setup.** Customer addresses are the inbound sender plus the inbound recipients (lower-cased; `cc` is not included). `allowed_domains` are the domains of those addresses plus `OUTPUT_GUARD__EXTERNAL_DOMAIN_ALLOWLIST` (default empty). If the sender is on a public provider such as gmail.com, every address at that provider counts as an allowed recipient domain.

**Stage 1 - sensitive patterns.** Source: `configs/pii_patterns.yaml`, 16 regexes, compiled case-insensitive; a value is "matched" per regex hit. Each hit becomes a finding and, if the pattern has `redact: true` and `OUTPUT_GUARD__REDACT_PII` is true, a redaction.

Exemptions:

- Credit-card hits that fail the Luhn checksum (needs at least 13 digits) are skipped.
- `pii.email` equal to a customer address is skipped.
- Any `pii.*` hit whose exact text also appears in the inbound email is skipped (the customer supplied it).
- `secret.*` and `leak.*` are never exempted.
- Emails and phone numbers that come from the knowledge base (kept chunks) are not exempted. Only URLs (stage 6) are checked against chunk text.

| Rule id (`kind`) | Targets | Severity -> score | Redacted |
|---|---|---|---|
| `secret.aws_access_key` | AWS key `AKIA`/`ASIA` + 16 characters | critical 0.97 | yes |
| `secret.openai_key` | `sk-...` (20+ characters) | critical 0.97 | yes |
| `secret.github_token` | `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_` + 36+ characters | critical 0.97 | yes |
| `secret.slack_token` | `xox[baprs]-...` | critical 0.97 | yes |
| `secret.private_key` | `-----BEGIN... PRIVATE KEY-----` | critical 0.97 | yes |
| `secret.jwt` | three dot-separated base64url parts starting `eyJ` | high 0.88 | yes |
| `secret.password_assignment` | `password/passwd/pwd/passcode/api key/secret/token`, then `:` or `=`, then 6+ non-space characters | high 0.88 | yes |
| `secret.otp_code` | `otp / one-time code / one-time password / verification code / 2fa code`, then up to 20 non-digit characters, then a 4-8 digit number | high 0.88 | yes |
| `pii.credit_card` | 13-19 digits, spaces or dashes allowed, Luhn-valid | critical 0.97 | yes |
| `pii.ssn_us` | US SSN `ddd-dd-dddd`, invalid ranges excluded | high 0.88 | yes |
| `pii.iban` | IBAN-shaped string | high 0.88 | yes |
| `pii.phone` | generic international phone shape (also fires on some space-separated number lists) | medium 0.6 | yes |
| `pii.email` | any email address | medium 0.6 | yes |
| `pii.vn_id_card` | 12 digits starting `0` (Vietnamese citizen ID) | medium 0.6 | yes |
| `leak.internal_marker` | `INTERNAL ONLY`, `CONFIDENTIAL`, `DO NOT SHARE`, `internal note`, `staff only` (any case, so the ordinary word "confidential" triggers it) | medium 0.6 | no |
| `leak.system_prompt_phrase` | "you are a helpful/customer support assistant", "system prompt", "my instructions are/say", "SECURITY RULES (channel isolation)" | high 0.88 | no |

Threat type of a pattern hit: `secret.*` gives `secret_leak`; `pii.*` gives `pii_leak`; `leak.system_prompt_phrase` gives `system_prompt_leak`; anything else (`leak.internal_marker`) gives `context_leak`. A pattern with an invalid regex or invalid severity is skipped with a log line. A missing YAML file disables this stage entirely (empty list, warning); the other stages still run.

**Stage 2 - protected-text leak** (rule `h-ngram-leak`, technique `prompt_extraction`). Lower-case both texts, keep only `[a-z0-9]+` tokens, and form 8-word n-grams (`OUTPUT_GUARD__SYSTEM_PROMPT_LEAK_NGRAM`, default 8, minimum 3). If the draft shares at least one 8-gram with a protected text, L4 emits `system_prompt_leak`, HIGH, score 0.9, detector heuristic, one finding per overlapping protected text. Not redacted.

**Stage 3 - citation integrity** (rule `h-citation-mismatch`, technique `citation_mismatch`). If the draft cites an id that is not in `allowed_citations` (exact, case-sensitive string match, no whitespace stripping), L4 emits `citation_hallucination`, MEDIUM, 0.6, sets `citation_mismatch=true` and lists `unknown_citations`. The check is skipped when `allowed_citations` is empty.

**Stage 4 - injected-goal compliance** (rule `h-injected-goal`, technique `injected_goal_compliance`). A hit is any of:

- an Inbound Guard indicator address appears (as a substring) anywhere in the lower-cased draft body or in `draft.recipients`;
- an Inbound Guard indicator URL appears in the body;
- a 6-word n-gram is shared between any `injected_instruction` and the body.

Any hit gives `injected_goal_compliance`, HIGH, 0.95, and `complied_with_injected_goal=true`. There is no exemption for addresses or URLs the customer legitimately wrote.

**Stage 5 - unsafe action** (rule `h-unsafe-action`, technique `exfiltration_email`). Fires if `draft.action == "forward"`, or if any `draft.recipients` entry is neither a customer address nor at an allowed domain. Emits `unsafe_action`, HIGH, 0.9. The actions `escalate` and `no_reply` are not inspected.

**Stage 6 - external link** (rule `h-external-link`, technique `link_insertion`). For each `http(s)://` URL in the body (trailing `.,;)` stripped; the URL character class stops at whitespace, quotes, `<>`, `)` and `]`), the URL is skipped if it appears in the kept chunk texts or in the inbound email, or if its host is exactly an allowed domain. There is no subdomain matching: `www.example.com` is flagged for a sender at `example.com`. Otherwise L4 emits `prompt_injection`, MEDIUM, 0.6.

**Merge.** Redactions are applied right to left; a redaction that overlaps one already applied is dropped, so the later-starting match wins an overlap, not the more severe one. Verdict score = maximum finding score (rounded to 4 places; 0 if no findings). Severity comes from `Severity.from_score(score)` with fixed defaults:

| Score | Severity |
|---|---|
| >= 0.97 | critical |
| >= 0.85 | high |
| >= 0.50 | medium |
| >= 0.20 | low |
| below 0.20 | none |

These L4 cut-offs are not config keys; the Inbound Guard's `flag_threshold` and `block_threshold` are not passed in.

**Stage 7 - optional LLM judge.** It runs only if all of these hold: `OUTPUT_GUARD__LLM_ENABLED` is true (default false); a model is registered for `GUARD_MODELS__OUTPUT_JUDGE`; the cheap verdict has no error; its severity is below HIGH; and there are injected instructions OR the score is >= 0.2. It sends the neutral intent, up to 5 attacker goals, and the redacted draft (first 8000 characters, wrapped in nonce markers) to the model, tier FAST, max 400 tokens, prompt `output_judge.v1`. It expects `{safe, confidence, violations[], rationale}`; `safe` and `confidence` (0 to 1) are both required, with no invented default, so an answer without `confidence` is a schema failure and fails closed as `judge_invalid:output`.

If `safe=false`: the unsafe score equals `confidence`; the verdict score becomes max(cheap score, that); one finding is added (detector `llm`, rule id `llm:<first violation, lower-cased, or "other">`, severity `from_score(confidence)`). The threat type is mapped from the exact labels `injected_goal_compliance / system_prompt_leak / pii_leak / secret_leak / unsafe_action`; any other label, including the prompt's own `off_topic` and `other`, maps to `unsafe_action`. If `safe=true` nothing is added: the LLM can only raise the score, never lower it. When the LLM answers validly, `decided_by` becomes `llm`.

**Config keys** (`OutputGuardSettings`, env form `OUTPUT_GUARD__<KEY>`):

| Key | Default |
|---|---|
| `PII_PATTERNS_PATH` | `configs/pii_patterns.yaml` |
| `REDACT_PII` | true |
| `SYSTEM_PROMPT_LEAK_NGRAM` | 8 (minimum 3) |
| `LLM_ENABLED` | false |
| `EXTERNAL_DOMAIN_ALLOWLIST` | `[]` |

Related keys: `GUARD_MODELS__OUTPUT_JUDGE` (model for stage 7) and `MAILGUARD__FAIL_CLOSED` (true). Hard-coded and not configurable: the 6-gram size in stage 4, all heuristic scores above, the 0.2 LLM trigger, and the 8000-character / 400-token / 5-goal limits.

## 3b. The outbound decision and the inbound floor

After the scan, `report.output_guard` is set and the shared policy engine decides at stage `outbound` (facts add `complied_with_injected_goal`, `citation_mismatch`, `redactions`; rules P03, P06, P07 and P10 apply here). Then `merge_floor` carries the inbound decision in:

- A **floor** exists only when the inbound decision came from a specific rule (not the catch-all `default` or a rule whose `when` is empty, read from the loaded policy) **and** its action is stricter than `auto_send`. So a clean inbound `draft_only` does not suppress a clean `auto_send`, and an inbound `auto_send` is never a floor. The floor is code, not configuration.
- Final action = the strictest of outbound and floor; `requires_human` = OR; risk tier = the higher. The floor wins only when strictly stricter (a tie goes to the outbound decision); the winner names `matched_rule_id`; `matched_rule_ids` is the union, inbound ids prefixed `inbound:`.
- `metadata.decided_by` is `output_guard`, or `inbound_gate` when the floor carried the decision (`carried_from_inbound = true`); `metadata.inbound_floor` names the floor rule and action.
- The auto-send allowlist (`acknowledgement`, `scheduling`) is applied after the merge, so `auto_send` happens only without a floor and inside the allowlist.
- The merged decision is the one audited; its `audit_id` is deterministic (a hash over the outbound id and the floor id).
- With the inbound gate off (C3-L3) there is no floor but the job still gets a final decision after generation (ADR-0015 intended change 6).

## 4. Output

`inspect` returns `(OutputVerdict, PolicyDecision)`.

| `OutputVerdict` field | Meaning |
|---|---|
| `layer` | `output_guard` |
| `severity`, `score`, `decided_by` | from the maximum finding score (`rule`, `llm`, `error`) |
| `findings` | all evidence, each with `detector` (`rule`, `heuristic`, `llm`) and a rule id |
| `original_text`, `redacted_text`, `redactions` | the draft, the masked draft, and one `Redaction` per masked value (kind, replacement, span, SHA-256 of the original) |
| `citation_mismatch`, `unknown_citations`, `complied_with_injected_goal` | flags read by the policy |
| `error`, `latency_ms`, `metadata` | failure, time, `llm_*` fields |

The `PolicyDecision` is the final decision (`report.decision`); see `L3-inbound-gate.md` for its fields. Threat types the scanner can emit: `secret_leak`, `pii_leak`, `system_prompt_leak`, `context_leak`, `citation_hallucination`, `injected_goal_compliance`, `unsafe_action`, `prompt_injection` (external link), `internal_error`.

## 5. Hands on to later layers

The final decision goes to the host (`decision_to_job_result`, which keeps every field and adds `reason`, `decided_by` and `guard_schema_version`) and the host review queue. The host sends only on `auto_send`. The redacted draft replaces the draft body unless the layer failed closed (empty body).

## 6. Who acts on it

L4 is the only place that changes the outgoing text (redaction) and the only layer after generation. Block and quarantine here do not undo the model call; they stop the draft from leaving. In the benchmark, an exit point `output_guard` means the final action is stricter than `draft_only` or `auto_send` after generation; `carried_from_inbound` reports separately that the floor decided.

## 7. Failure behavior

- **Exception in the cheap stages:** caught and logged; an error verdict with severity HIGH, score 0.9, one `internal_error` finding, `decided_by="error"`, `error` set. With `MAILGUARD__FAIL_CLOSED` true (default) `redacted_text` is empty, so the draft body is replaced with nothing. With it false the verdict is severity NONE and the original text is kept. P00 asks for human review either way.
- **The output judge** (optional, stage `output`): when configured and it fails or answers invalidly (timeout, HTTP error, empty choices, non-JSON, schema failure after one repair call), the cheap verdict is kept, `llm_fallback*` metadata is written and, with `MAILGUARD__FAIL_CLOSED_ON_JUDGE_ERROR` true (default), `error = judge_invalid:output`, so P00 asks for human review. With the flag false the failure is invisible to the policy (the v2 visible fallback). A judge that is not configured is not an error.
- **Missing patterns file:** stage 1 becomes empty with a warning; the other stages still run.
- A non-`LLMError` exception inside the LLM stage (for example a missing prompt file) is not caught by the scanner.

## 8. Cost and speed

- Cheap stages: pure CPU string work, no model, no network (the source states 1 to 5 ms; not measured for v3).
- The LLM stage (off by default): at most one call (two with a repair), FAST tier, 400 output tokens, input at most 8000 characters of draft plus goals. Guard call stage `output`; the guard-wide budget is five calls per job (`MAX_GUARD_CALLS_PER_JOB`), and L4 makes no generation call, so it adds nothing to rag-email's "one generation call per job".

## 9. Blind spots

1. **Body only.** It scans `draft.body` only. `draft.subject` is never inspected (moot in rag-email, where `reply.v1` has no subject field).
2. **Pattern matching, no meaning.** A paraphrased secret or system prompt passes; the system-prompt check needs 8 identical consecutive tokens after lower-casing and dropping punctuation. Non-ASCII letters are separators in the `[a-z0-9]+` tokenizer, so Vietnamese words shatter into ASCII fragments. A verbatim copy is still caught (tested), but on shorter real-word spans (about 3-4 words per 8-gram, about 2 per 6-gram), which makes stages 2 and 4 more false-positive prone on accented text. Diacritic-changed or paraphrased copies are not caught.
3. **Narrow PII coverage.** Only what the 6 PII regexes cover: cards, US SSN, IBAN, generic phone, email, 12-digit VN id. Names, postal addresses, order numbers, health data, and other customers' records with no such shape are invisible.
4. **Customer-supplied values are exempt.** Any `pii.*` value that also appears in the inbound email is not reported, including text an attacker put in the email.
5. **Injected-goal detection knows only literal strings.** These are addresses and URLs from the inbound email, and 6-word phrases from instructions the Inbound Guard or the Doc Scanner extracted. An attack whose goal is pure text ("tell the customer the refund is approved") with no address, no URL and no extracted instruction is invisible. Instructions shorter than 6 words produce no n-grams and can never match. Obfuscated addresses ("attacker [at] evil dot test") do not match the plain substring test. If L1 or L2 is off, their contributions are empty.
6. **Stage 4 has no exemption.** Inbound Guard indicators are every address and URL in the inbound text, including the customer's own From address. A draft that repeats any of them (the customer's address, a tracking link the customer pasted, a colleague's address) is flagged `injected_goal_compliance` (HIGH), and the outbound policy blocks it (P03). Listing the customer in `draft.recipients` also triggers it, but rag-email's `reply.v1` has no recipients field, so only the body-echo path exists there. A code-level cross-check showed a draft with `recipients=["alice@example.com"]` and body `Hi Alice, thanks.` is flagged.
7. **The sender's own domain is allowed.** The sender's and recipients' domains are on the allow-list. If the attacker is the sender, links to and recipients at the attacker's own host pass stages 5 and 6 unless the exact address or URL is an Inbound Guard indicator caught by stage 4. If the sender is on a public provider such as gmail.com, every gmail.com address is an allowed recipient domain. Stage 6 matches the host exactly (no subdomains), and `cc` addresses are not treated as customer addresses.
8. **Link check sees only `http://` and `https://` URLs.** Bare domains, `www.` links and `mailto:` are not seen. Only kept chunk text and the inbound email are trusted for URLs; URLs from `business_data`, the thread summary or recent messages are not, so they would be flagged (v1 passes `business_data=None`).
9. **Citation check compares ids only.** It is exact, case-sensitive, with no whitespace stripping; it does not check whether the cited chunk supports the claim, and it is skipped entirely when no chunk was kept. rag-email's own `verify_citations` casefolds and strips ids, so L4 can report a mismatch that rag-email does not.
10. **Action check is thin in rag-email.** It reads only `draft.action == "forward"` and `draft.recipients`. `reply.v1` never populates `recipients`, so stage 5 fires only on `action == "forward"`.
11. **Content quality is out of scope.** Wrong facts, toxic or off-topic text are not checked unless the optional LLM judge runs.
12. **Sees the final text only.** It cannot see tool calls or anything the model did before writing the draft.
13. **Pattern-stage false positives.** Emails and phone numbers that come from the knowledge base are redacted (only URLs are trusted from chunks) and make the verdict MEDIUM (P08 draft_only). `pii.phone` fires on space-separated number lists (v1 run `2026-09-29-qwen25`: 4 findings in 2 rows on ASCII-code lists, body rewritten). The word "confidential" in any case triggers `leak.internal_marker`.
14. **Sensitive values stay in the verdict.** Finding excerpts (match plus 40 characters each side) and `original_text` hold the unredacted text, so persisting the L4 verdict persists the secret.


## 10. Why it is needed even with the other layers

- **No earlier layer sees the draft.** Only L4 has the model's output, so anything that happens at generation time is invisible to the others: an invented citation, `action=forward`, a quoted system prompt, a secret pasted from a retrieved chunk.
- **L1 is probabilistic and a high score does not stop generation by itself**; the gate stops only critical or high-with-exfiltration verdicts. L4 asks a different question: did the assistant do or leak something it must not, even if the email looked benign.
- **Only L4 changes the outgoing text**, so even when the policy allows a draft, secret and PII shapes are masked.
- **It holds the final word.** With the floor, the final action is never weaker than the inbound decision, and with the inbound gate off the job still gets a decision.
- **Limit:** it relies on L1 and the Doc Scanner (L2) for its injected-goal inputs, so it is not independent of them for that stage, and it does nothing for text-only attacks (blind spot 5).

## 11. v1 results (pre-v3)

The v1/v2 benchmark rows of the old "L4 output scanner" and the old outbound policy step are history; their counts are not comparable (the outbound decision now merges the inbound floor, and L4 findings use the v3 hand-offs). Re-read the committed folders with `make mailguard-report RUN=<id> LEGACY=1`; the v3 numbers come from Task 16.

## 12. Worked example (traced from the code, no model run)

An email gets inbound decision `human_approval` from P05 (a HIGH finding, no exfiltration type). The model writes a clean acknowledgement draft with no leaks.

1. Scanner: no findings, severity none, nothing redacted.
2. Outbound policy (stage outbound, category `acknowledgement`, action `reply`, severity none): P10 matches (auto_send) and P99 is only the fallback, so the outbound decision is `auto_send`.
3. Floor: the inbound decision came from the specific rule P05 and `human_approval` is stricter than `auto_send`, so it is a floor. It is strictly stricter, so it wins: final action `human_approval`, `requires_human` true, `matched_rule_id = P05-...`, `matched_rule_ids = [P10-..., inbound:P05-..., inbound:...]`, `metadata.decided_by = inbound_gate`, `carried_from_inbound = true`.
4. The job is not auto-sent: a reviewer sees the draft. An email whose inbound decision was only the catch-all `draft_only` would have had no floor and would have been auto-sent.

## 13. Sources

Code (`agentmailguard/`, without line numbers): `mailguard/layers/output_guard/guard.py` (floor rules in its docstring), `scanner.py`, `mailguard/policy/engine.py`, `mailguard/contracts/verdict.py` (`OutputVerdict`, `Redaction`), `mailguard/contracts/policy.py`, `mailguard/config/settings.py` (`OutputGuardSettings`), `configs/pii_patterns.yaml`, `configs/policy.yaml`, `mailguard/prompts/output_judge.v1.txt`, `mailguard/llm/judge_calls.py`, `tests/unit/test_output_guard_floor.py` (the floor invariants), `docs/adr/0015-guard-architecture-v3.md` (decisions 1, 3, 5).
