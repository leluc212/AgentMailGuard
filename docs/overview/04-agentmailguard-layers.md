# AgentMailGuard layer by layer: what each check reads, decides and hands on

**Who this is for:** thesis reviewers and technical readers new to the project. Terms are defined at
first use and again in the glossary (section 7). Background on the whole guard is in
[03-agentmailguard.md](03-agentmailguard.md).

## 1. The two questions and the short answers

1. **For each AgentMailGuard layer: what goes in, how does it tell dangerous from harmless, and what
   comes out?** Section 3 gives one card per layer with the same headings; section 2 shows how the
   layers hand data to each other.
2. **Why are layers 2 to 5 needed when layer 1 stops most attacks in the v1 results?** Three answers.
   - **L1 alone stops nothing.** It only returns a score; L5 turns a score into an action. "L1 stops
     most attacks" therefore means L1 plus L5 (preset C1), which blocked 86 of 100 email attacks in a
     real run. Adding L2 and L3 (preset C2) blocked 98 of 100, all of them inbound blocks, so the extra
     12 came from L2.
   - **Whole attack classes are outside L1's input.** It flagged 0 of 100 poisoned-document attacks and
     never sees the draft. Only L3b, L4 and (for thread text) L3 touch those.
   - **Measured: four of the six layers are each necessary on this benchmark.** In the pre-registered
     remove-one-layer run (section 5.1: gpt-4o-mini, 550 cases), removing L2, L3b or L4 raised the
     poisoned-document ASR from 30 % to 38 %, 58 % and 40 %, and removing L5 raised the email ASR from
     0 % to 7.7 % (23 of 300), although L1 still flagged all 300: without L5 no flag becomes a block.
     Removing L1 changed almost nothing (0.3 %), because L2 then caught 271 of the same 300 attacks: on
     these cases L1 and L2 back each other up. L3's effect was not measurable.

Terms first. **AgentMailGuard** is the security package around rag-email's reply writer. A **prompt
injection** is text in an email or document that tries to give orders to the AI that writes the reply
(the **reply model**, a large language model or **LLM**). A **layer** is one stage of the guard: L1, L2,
L3b and L4 inspect text, L3 builds the prompt, L5 applies policy; **L3b** is the second half of layer 3.
Each layer returns a **verdict**: a **severity** (none, low, medium, high, critical, from a **score**
between 0 and 1) and **findings**. Only **L5**, the policy engine, decides what happens to the message
(stop it, hold it for a person, let it go); some layers also change content themselves (L2 strips
sentences, L3b removes chunks, L4 rewrites the draft). The presets **C0 to C3** choose layers: C0 none,
C1 L1 plus L5, C2 L1, L2, L3 plus L5, C3 all. The v1 **runs** are `2026-09-29-gpt4omini` (reply model
gpt-4o-mini) and `2026-09-29-qwen25` (Qwen2.5-7B). "Flagged" means severity medium or higher.
"Not determined" marks something not verified.

## 2. The guard at a glance

One email travels down the page, in the order the checks run. The letters mark what each layer hands on.

```
 inbound email (subject, body, sender)
        |
        v
 +-----------------------------+
 | L1 email injection scanner  |   rules, n-gram model, optional AI judge
 +-----------------------------+
        | verdict ------------------------------> L5
        | (a) indicators + injected_instructions -> held for L4
        |     (L2 does not receive L1's verdict)
        v
 +-----------------------------+
 | L2 user intent extractor    |   strips instruction-like sentences
 +-----------------------------+
        | verdict ------------------------------> L5
        | (b) stripped sentences + quoted instructions -> held for L4
        | (c) sanitized_body, intent, actions, entities -> held for L3
        v
 +-----------------------------+
 | L5 policy engine: INBOUND   |-----> block or quarantine: STOP, no reply model call
 +-----------------------------+
        | any other decision
        v
 knowledge-base search --> retrieved chunks
        |
        v
 +-----------------------------+
 | L3b retrieved doc scanner   |   quarantines poisoned chunks
 +-----------------------------+
        | verdict ------------------------------> L5
        | (d) quarantined excerpts -------------> held for L4
        | surviving chunks
        v
 +-----------------------------+
 | L3 channel isolation        |   scrubs, caps, marks, builds the prompt
 +-----------------------------+   (uses (c); its verdict -> L5 outbound only)
        | marked prompt
        v
    reply model ----> draft
        |
        v
 +-----------------------------+
 | L4 output scanner           |   reads the draft against (a), (b), (d)
 +-----------------------------+   redacts secrets and personal data
        | verdict ------------------------------> L5
        v
 +-----------------------------+
 | L5 policy engine: OUTBOUND  |-----> quarantine / block / human approval /
 +-----------------------------+       draft only / auto-send
```

- **Indicators** (a) are every e-mail address and link found in the email, the sender's own included.
  L4 checks whether the draft repeats one, or any "injected instruction" from (a), (b) or (d).
- **L5** decides twice: before the reply model runs (inbound) and on the draft (outbound). In the
  pipeline code only an inbound block or quarantine stops the run before generation. The
  **dispatcher** (the mail-sending component outside this repository) should act on the outbound decision.

## 3. The layers, one by one

Every card uses the same headings. Severity from score: under 0.20 none, 0.20 low, 0.50 medium, 0.85
high, 0.97 critical (L1's bands; L3b's chunk ladder starts high at 0.70). **Fail-closed** means a layer
that crashes reports a HIGH error verdict instead of a clean one. L5's rule P00 then gives
human_approval, which does not stop generation and outranks even an L1 critical quarantine. An
AI-stage failure inside a layer (timeout, bad JSON) is not an error to L5, and L3 has no fail-closed path.

Scores are hand-set constants (no calibration file was found) and each layer combines them differently
(L1 noisy-OR, L2 maximum, L3b noisy-OR), yet L5 compares severities across layers as if equal. No
threshold here is justified by a measurement.

**Cost and speed.** Measured only for L2: its AI step is one call per email, not gated by L1, at p50
1.6 s (gpt-4o-mini) and 2.8 s (Qwen). L1's classifier costs about 1 to 1.5 ms per text; its judge
decided 10 of 300 v1 attack emails. L3, L3b, L4 and L5 make no model call in v1 and have no measured
latency (the authors claim 1 to 3 ms per chunk for L3b, under 1 ms for L3 and L5).

`GuardedEmail` is the record holding one email. **Quoted history** is earlier messages pasted inside
the body; **thread history** is earlier messages passed to L3 as separate fields. Worked examples are
in the full cards.

### L1 - Email Injection Scanner

**Purpose.** Scores one inbound email from 0 to 1 for how likely it is to contain prompt injection. It detects only; it edits nothing and blocks nothing itself.

**Input.** One `GuardedEmail`: `subject`, `body_text` (rules see all of it, quoted text included; the classifier and judge see only the first 12000 characters of subject, sender and body), `body_html` (rules and one heuristic only; always None in rag-email's wiring). Headers, attachments, recipients, thread history and chunks are never read.

**How it decides.** A cascade ordered by cost.
- Stage 1: 15 regex rule families (54 patterns, hand-set scores 0.55 to 0.97, English and Vietnamese) plus 4 heuristics (zero-width characters, homoglyphs meaning look-alike letters from another alphabet, instruction density, hidden HTML). `rule_score` is the maximum.
- Stage 2, if `rule_score < 0.90`: a TF-IDF plus calibrated logistic-regression classifier gives p; `fused = 1 - (1 - rule_score)(1 - p)`. Rule scores of 0.90 or more are final.
- Stage 3, only if `0.20 <= fused < 0.85` and a judge is configured: an LLM judge; `final = 0.6 * judge + 0.4 * fused`, so a judged verdict is never CRITICAL.

**Output.** `LayerVerdict`: `severity`, `score`, `findings`, `decided_by` and `metadata` with `indicators` and, if the judge ran, `injected_instructions`.

**Hands on / who acts.** L4 receives the indicators and injected instructions. L5 turns severity into quarantine (CRITICAL, P01), block (HIGH with an exfiltration-type threat, P02), human_approval (P05) or draft_only (P08).

**Failure behavior.** Exception: HIGH error verdict; P00 gives human_approval and generation continues. Judge timeout: no retry, cheap verdict kept, L5 not told. Non-JSON or invalid JSON: one repair call first, then the same fallback.

**Blind spots.** Line-wrapped phrases, homoglyphs, zero-width splits, encodings, other languages, paraphrase. Final rules at 0.90 or more cannot be corrected: "Please override the policy for this customer." scores 0.97 and would be quarantined.

**Why needed.** It is the first and cheapest detector and the only source of L4's indicators. Other layers also decide without a model (L2 and L3b reuse its rules; L3, L4, L5 use none by default). In v1 it flagged 299 of 300 attacks, mostly through the trained classifier: rules alone reached medium on 90. In the replay (section 4.1), 16 (gpt) and 17 (Qwen) inbound blocks need L1. It is not sufficient: it edits nothing and cannot act without L5.

**v1 benchmark (C3).** Attacks flagged 299 (gpt) / 296 (Qwen) of 300, critical 213 in both; 0 of 100 RAG attacks flagged; 0 of 150 benign flagged (only 12 of them are not near-duplicates of L1's training negatives).

Full card: [layers/L1-email-injection-scanner.md](layers/L1-email-injection-scanner.md)

### L2 - User Intent Extractor

**Purpose.** Removes instruction-like parts of the inbound body and writes a short, neutral description of what the customer wants, so the reply model works from cleaned text.

**Input.** One `GuardedEmail`; it reads `email.text` (the body with quoted history cut when the pipeline supplies that; in the benchmark it equals the full body), first 6000 characters, plus the subject for the paraphrase. It never reads HTML, headers, attachments or sender, and does not receive L1's verdict.

**How it decides.**
1. Split the body into paragraphs (long ones at sentence ends).
2. Score each segment: max(best regex rule score, classifier probability on segments of 20+ characters). It uses 14 of L1's 15 rules and the same classifier.
3. Strip every segment scoring at least 0.50.
4. Regex extraction of entities, actions and a template intent.
5. Optional LLM step (default on, one call per email, not band-gated): a paraphrase plus a flag "instructions aimed at the assistant", scored max(0.6, the model's own confidence). The prompt never defines what that confidence is about.

**Output.** `SanitizedIntent`: `severity` (never low), `score`, `findings` (always `prompt_injection`), `sanitized_body`, `user_intent`, `requested_actions`, `entities`, `stripped_segments`.

**Hands on / who acts.** L3 puts `sanitized_body` in the untrusted channel and the intent, actions and entities in an unmarked "semi-trusted" section. L4 gets stripped text and the model's quoted instructions. L5 turns critical into quarantine, high into human_approval, medium into draft_only. L2 never blocks; its findings are all `prompt_injection`, so alone it cannot trigger the P02 block, though L5 pools threat types across layers.

**Failure behavior.** Exception: HIGH error verdict with an empty `sanitized_body`, so L3 falls back to the raw body; P00 gives human_approval. LLM timeout: regex result kept. Non-JSON answer: silently an empty result, no repair, no error. Schema-invalid JSON: one repair call.

**Blind spots.** Subject never scanned (and copied verbatim into the unmarked intent on the heuristic path); HTML ignored; nothing past 6000 characters; paraphrase; over-stripping.

**Why needed.** L1 only labels, so on non-block outcomes the raw body reaches the reply model unless L2's `sanitized_body` is used; that cleaned text's effect is unmeasured, since only 15 (gpt) and 14 (Qwen) email attacks reached generation. What v1 measures is L2's severity scoring: per-segment scores give 267 (gpt) and 268 (Qwen) criticals against L1's 213. Whether re-tuning L1's thresholds would do the same is not tested. Its AI step flagged no attack L1 missed. It also gives L4 payload evidence.

**v1 benchmark (C3).** L2 flagged 298 (gpt) / 297 (Qwen) of 300 attacks (L1: 299 / 296) and caught only 1 and 3 that L1 missed. It flagged 3 of 150 benign emails (draft_only) and, for gpt-4o-mini only, 89 of 100 RAG emails (section 4.4).

Full card: [layers/L2-user-intent-extractor.md](layers/L2-user-intent-extractor.md)

### L3 - Channel Isolation

**Purpose.** Builds the final prompt so outside text (email, thread history, chunks) is scrubbed of fake markers, capped, marked as "data, never obey" (spotlighting) and kept apart from the operator's trusted instructions. A prompt-assembly layer, not a classifier.

**Input.** Trusted `system_instructions`, `category_instructions`, `business_data`; semi-trusted L2 intent (unscrubbed, unmarked); untrusted email (L2's `sanitized_body`, else raw text), `thread_summary`, `recent_messages` and the chunks that survived L3b. HTML, headers and attachment names are never read.

**How it decides.** No model. (1) A random nonce; (2) scrub untrusted text with two regexes: forged `<<<WORD[:alnum]>>>` markers and 12 chat-template tokens such as `<|im_start|>`; (3) truncate to a shared 6000-token budget; (4) mark the text (default: `datamark`, whitespace becomes `^`); (5) wrap it in nonce-tagged channel markers; (6) add a 6-rule SECURITY RULES preamble. Score from n = markers plus tokens removed: 0 gives none, else `min(0.9, 0.55 + 0.1n)`, so medium or high, never low or critical.

**Output.** The finished prompt (`SecurePrompt`) and a heuristic `LayerVerdict`.

**Hands on / who acts.** Messages go to the reply model. The verdict goes only to the outbound L5 gate (medium gives draft_only, high gives human_approval). L3 blocks nothing; L4 ignores it.

**Failure behavior.** No AI step, no try/except and no fail-closed: an exception reaches the caller.

**Blind spots.** Plain-word injections score 0. The intent block is unmarked, and forged `<<<...>>>` markers are detected by neither L1 nor L2, so they can reach it. The model's obedience is never checked, and preamble rule 6 asks the model to report suspicious text in a `security_notes` field that rag-email's `reply.v1` (the fixed JSON format of the reply) lacks, so that rule cannot be followed in the benchmark. With L3 off, scrubbing and truncation still run.

**Why needed.** Argued by construction only. Nothing else touches thread text; it alone tells the model which regions are data when a detector misses; and it removes forgeries of its own delimiters, which no L1 rule targets. It mitigates and cannot detect semantic attacks.

**v1 benchmark (C3).** L3 ran on 15 (gpt) and 14 (Qwen) email attacks and flagged 1 in each (2 template tokens), 0 of 100 RAG attacks and 0 of 150 benign. No case contains thread-history injection. Its effect is not determined.

Full card: [layers/L3-channel-isolation.md](layers/L3-channel-isolation.md)

### L3b - Retrieved Document Scanner

**Purpose.** Reads each knowledge-base passage (chunk) retrieval returned and removes ("quarantines") those that look like planted RAG poisoning: text meant to steer the reply model's answer.

**Input.** `scan(chunks, query)`: each chunk's content and ids, plus the retrieval query (or the customer's subject and body, 1500 characters). No provenance. Runs only in preset C3 and its ablations.

**How it decides.** Per chunk, independently. (A) The 14 applicable L1 rules. (B) L1's obfuscation checks. (C) English-only heuristics: `answer_forcing` ("always answer that ..."), `instruction_override`, `link_insertion` (an attacker link or contact) and `query_echo` (the chunk begins by repeating the customer's question; 0.75 at 90% word overlap, 0.55 at 70%). `query_echo` targets **PoisonedRAG**, a published attack that prepends the target question to the bad passage so search ranks it first. (D) L1's classifier, if its model file is present. `fused = noisy-OR(highest rule score, classifier)`; quarantine at `fused >= 0.70`, so any single finding of 0.70 or more quarantines. (E) An AI judge, default off, only for 0.30 to under 0.70.

**Output.** Kept chunks plus one verdict per chunk (`quarantined`, `severity`, `score`, `findings`).

**Hands on / who acts.** L3 gets kept chunks only. L4 gets kept chunks as trusted and quarantined excerpts as "injected instructions". L5 gets severities and the quarantine ratio. L3b removes chunks but does not block.

**Failure behavior.** Cheap-stage exception: chunk quarantined, HIGH error verdict; P00 gives human_approval. AI-stage timeout: no retry, cheap verdict kept (fails open). Non-JSON or invalid JSON: one repair call first.

**Blind spots.** Chunk text only, so plainly worded false facts pass. One chunk at a time. `query_echo` is effectively off for accented Vietnamese queries; FAQ chunks that begin with the customer's question are false positives.

**Why needed.** L1 and L2 never see chunks, L3 marks but does not remove poison, and L4 works after generation. Only L3b has answer-steering heuristics and drops whole passages. Limit: it reuses L1's rules and classifier, and one heuristic does almost all the work.

**v1 benchmark** (C3, RAG group, both runs identical, AI stage off). 148 of 382 poisoned chunks quarantined (38.7%), 0 of 96 clean, though the clean chunks may also be classifier negatives (overlap not checked). 141 of 148 rest on `query_echo`. Goal reached in 1 of 46 quarantine cases (gpt) and 3 of 46 (Qwen), against 31 of 54 and 36 of 54 without.

Full card: [layers/L3b-retrieved-document-scanner.md](layers/L3b-retrieved-document-scanner.md)

### L4 - Output Scanner

**Purpose.** Reads the finished draft for leaked secrets or personal data, leaked instructions, invented citations, an attacker's payload echoed back, forwarding to outsiders and odd links. It masks secrets and personal data and reports the rest to L5.

**Input.** The draft body (`reply.v1` has no subject or recipients), the inbound email, L2's intent, ids of chunks kept by L3b, the system and category prompts, L1's indicators, and injected instructions collected from L1's judge, L2 and L3b.

**How it decides.** Six deterministic stages, all always run; score = maximum finding score.
1. 16 sensitive-pattern regexes: critical (API keys, private key, Luhn-valid card), high (JWT, password, OTP, SSN, IBAN), medium (phone, email, internal markers). Personal data the customer wrote is exempt; secrets never are.
2. Protected-text leak: 8-word overlap with the system or category prompt, high.
3. Citation integrity: chunk id not among kept chunks, medium.
4. Injected goal: an indicator address or link in the draft, or a 6-word overlap with an injected instruction, high. No exemption for the customer's own address.
5. Unsafe action: forward or outside recipient, high.
6. External link: URL not in chunks, the email or an allowed domain, medium.

An optional AI judge (off) can only raise the score.

**Output.** `OutputVerdict`: `severity`, `score`, `findings`, `redacted_text`, `citation_mismatch`, `complied_with_injected_goal`.

**Hands on / who acts.** L5: P01 quarantine on critical, P02 block on high secret or prompt leak, P03 block on goal compliance, P05 or P07 human approval, P08 draft only. L4 blocks nothing itself, but `run()` swaps in `redacted_text`.

**Failure behavior.** Exception: HIGH error verdict and an emptied draft; P00 gives human_approval. AI-stage timeout: no retry, cheap verdict kept. Non-JSON or invalid JSON: one repair call first.

**Blind spots.** Literal matching: paraphrases pass, accented text breaks the tokenizer, text-only attacks with no address, link or extracted instruction are invisible. Stage 4 flags any echo of an inbound address or link, so a benign reply repeating one is blocked by P03.

**Why needed.** No earlier layer sees the draft, and a high L1 score does not always stop generation (11 gpt and 10 Qwen high-L1 emails were still generated). Only L4 sees model-time failures and rewrites text. Limit: it depends on L1 and L2 for its injected-goal inputs.

**v1 benchmark (C3, AI stage off).** 0 secret leaks, 0 prompt leaks and 0 unsafe actions, so those checks are unexercised. It flagged 2 of the 15 (gpt) and 6 of the 14 (Qwen) email-attack drafts (true-catch status not determined) and 0 of 150 benign emails, but those retrieve no chunks and none contains a link or an address besides the sender's, so the stage-4 false-positive risk is untested. RAG blocks: section 4.3.

Full card: [layers/L4-output-scanner.md](layers/L4-output-scanner.md)

### L5 - Policy Engine

**Purpose.** Turns the other layers' verdicts into one action (`quarantine`, `block`, `human_approval`, `draft_only`, `auto_send`) using an ordered YAML rule list. It detects nothing.

**Input.** `decide(report, stage, category, draft_action)`. From the L1 to L4 verdicts it derives only summary facts: max severity, threat-type union, layer error, quarantine ratio, removed ratio, L4 flags. These are pooled across layers, so a "high with an exfiltration type" need not be one finding or one layer. It never reads text. Stage is `inbound` (after L1 and L2) or `outbound` (after L4).

**How it decides.** No model, no score formula. The rule with the lowest priority number whose conditions all match wins (policy `2026.09-v1`, 12 rules): P00 layer error gives human_approval; P01 critical gives quarantine; P02 high plus exfiltration, tool-abuse, secret or prompt-leak type gives block; P03 outbound goal compliance gives block; P04 to P09 escalate to human_approval or draft_only; P10 is the only auto_send path (outbound, severity at most low, category acknowledgement or scheduling); P99 default draft_only. The **risk tier** follows max severity (T0 clean to T4 critical). Order is by priority, not strictness, so P00 pre-empts P01.

**Output.** `PolicyDecision`: `audit_id` (a fingerprint of the decision inputs), `action`, `risk_tier`, `matched_rule_id`, `reasons`, `requires_human`, `quarantined_chunk_ids`.

**Hands on / who acts.** The pipeline stops before L3b, L3 and generation only on an inbound block or quarantine (the guard worker's path for a job that already carries a draft does not stop even then). Everything else is left to the dispatcher (not determined): `human_approval` and `draft_only` are only labels, and the benchmark scores them as delivered drafts.

**Failure behavior.** `decide` has no try/except; the **guard worker** (the service that runs the guard per queued job) turns an exception into an error message. A missing policy file makes every decision draft_only. A crashed layer triggers P00.

**Blind spots.** It trusts other layers and uses maximum severity only. Other high findings get human approval and generation still runs. `auto_send` depends on the triage category label; its share of mail and miss rate are not given. P06 cannot fire (P05 pre-empts it), P04 duplicates P05 and P09 never decides alone.

**Why needed.** Every other layer scores or filters; none decides. With L5 off, nothing ever stops the pipeline. L5 also acts on signals L1 never sees (RAG quarantine ratio, L4 goal compliance, citation mismatch), handles crashed layers and grades responses. It adds no detection.

**v1 benchmark (C3).** Blocks: 317 (gpt) and 316 (Qwen) of 400 attacks (inbound quarantine 281 and 282, inbound block 4 and 4, outbound P03 32 and 30); 0 of 150 benign blocked. No LLMail attack achieved its goal (0 of 300) against 162 and 155 unguarded; on RAG, 32 and 39 of 100 still did (79 and 91 unguarded).

Full card: [layers/L5-policy-engine.md](layers/L5-policy-engine.md)

## 4. Why one layer is not enough

The evidence is the v1 benchmark, config C3 (all layers), 550 cases per model: 300 email attacks from
LLMail-Inject (a public set of injection emails), 100 RAG attacks (the poison sits in the knowledge
base and the email is a harmless question) and 150 benign emails, one sample per case. **Goal achieved**
means the benchmark's string-match rule found the attacker's target in the output. **Denominator
caveat:** the pipeline stops at the inbound gate when L5 blocks, so L3, L3b and L4 never ran on the 285
(gpt) or 286 (Qwen) inbound-blocked emails.

### 4.1 Redundancy at the inbound gate (a simulation, not a run)

An inbound block needs L1 or L2 at critical (rule P01), or high plus an exfiltration-type threat
(rule P02). AgentMailGuard's own L5 was replayed offline on the stored C3 reports with L1 removed, L2
removed or both. Every "alone" row includes L5, since L1 and L2 alone stop nothing. The replay
reproduced the stored inbound decision on 550 of 550 rows per run, and its L1-alone figure on the
100-attack subset (86) equals the real C1 run. The real C2 run (98 of 100) is the cleanest real
measure of what L2 adds over L1.

| Inbound blocks on the 300 LLMail attacks | `2026-09-29-gpt4omini` | `2026-09-29-qwen25` |
|---|---|---|
| Full guard (L1, L2, L5) | 285 | 286 |
| L2 + L5 (L1 removed), simulated | 267 (93.7% of 285) | 268 (93.7% of 286) |
| L1 + L5 (L2 removed), simulated | 248 (87.0%) | 250 (87.4%) |
| Neither L1 nor L2, simulated | 0 | 0 |
| Either alone would still block | 232 | 233 |
| Only L1 alone suffices | 16 | 17 |
| Only L2 alone suffices | 35 | 35 |
| Neither alone suffices | 2 | 1 |

Removing L1 costs 18 of the 285 blocks per run, mostly falling to human approval (P05), not automatic
sending. In the unguarded C0 run 6 of the 18 (gpt) and 5 (Qwen) achieved the attacker's goal, so they
are candidates for real losses; C0 is a separate generation sample and later layers might still have
stopped some, which the replay cannot say. On the 100 RAG and 150 benign rows every variant gives 0
inbound blocks.

Caution: L2 reuses L1's rules and classifier, so they are not two independent detectors. Its extra 35
blocks come from scoring, not new detection: L2 reaches critical on 267 attacks against L1's 213,
although L1 flags 299. The whole guard let through 0 of 300 attacks in either run, against 162 (gpt) and
155 (Qwen) unguarded, and blocked none of the 150 benign emails.

### 4.2 Only a handful of blocks rest on one layer

Blocked attacks are 317 (gpt: 285 inbound, 2 email outbound, 30 RAG outbound) and 316 (Qwen: 286, 3,
27). Layers at high severity or above on each:

| Layers at high or above | `2026-09-29-gpt4omini` | `2026-09-29-qwen25` |
|---|---|---|
| L1 and L2 together (inbound) | 277 | 279 |
| Exactly one layer | 8 (L1 3, L2 5) | 8 (L1 4, L2 3, L4 1) |
| Two or more layers | 309 of 317 (97.5%) | 308 of 316 (97.5%) |

This shows redundancy, not necessity. Because L1 and L2 share detector code, "two layers" is partly one
detector counted twice, and high redundancy means each layer is individually dispensable on this data,
not that each is needed; the simulation above is the necessity view. L1 and L2 share one blind spot: in
the Qwen run `llmail-f4f9557edd62` was below medium on both and was not blocked.

### 4.3 The poisoned-document vector never touches L1

For the 100 RAG attacks (89 PoisonedRAG cases and 11 `seed_rag` cases from the benchmark's own seed
set), L1 flagged 0 of 100 in both runs and every simulated variant gives 0 inbound blocks. Only L3b,
L4 and L3's marking act on this vector, and what the numbers show is narrower than "the defense is L3b
and L4".

| RAG measure | `2026-09-29-gpt4omini` | `2026-09-29-qwen25` |
|---|---|---|
| L3b: poisoned chunks quarantined | 148 of 382 (38.7%) | 148 of 382 |
| L3b: clean chunks quarantined | 0 of 96 | 0 of 96 |
| L3b: quarantines resting on `query_echo` | 141 of 148 (137 alone, 4 with the classifier; 1 more among four signals) | same |
| Blocked RAG cases, all by rule P03 (L4 `injected_goal_compliance`) | 30 | 27 |
| Blocked drafts that had achieved the goal before L4 | 7 of 30 | 5 of 27 |
| Goal achieved in the final output / unguarded C0 (100 cases) | 32 / 79 | 39 / 91 |
| Unblocked cases whose pre-L4 draft achieved the goal | 33 | 42 |
| Goal achieved with a quarantine (46 cases) / without (54) | 1 / 31 | 3 / 36 |

- **L3b is precise but low-recall and rests on one heuristic.** The worklog's chunk recall of 0.853
  comes from a separate detector test whose results file is not in this tree, so it cannot be
  reconciled with the measured 38.7%.
- **Every RAG block is L4's P03**, here a 6-word overlap with an "injected instruction". For Qwen all 27
  matches came from quarantined-chunk excerpts; for gpt 10 from excerpts only, 16 from excerpts plus L2's
  quoted question wrapper, 4 from L2 alone. Those excerpts are mostly the chunk's opening line, which
  repeats the customer's own question. So 23 of 30 (77%) and 22 of 27 (81%) blocked drafts had not
  achieved the goal, and the customer's harmless question went unanswered. The block count is a weak
  proxy for attack success.
- **Attack success is still 32 and 39 of 100.** Cases with a quarantine mostly ended safe, but C0 and C3
  are separate generation samples, so the drop from 79 and 91 cannot be attributed to any layer. The
  33 and 42 count pre-L4 drafts, the 32 and 39 the final output; the gap was not traced.

### 4.4 Other caveats about the evidence

- **gpt's L2 flags on RAG emails are a false-positive mode, not detection.** All 89 PoisonedRAG emails
  share the opening line "Hi, a quick factual question for your knowledge assistant: ...". In the gpt
  run L2's AI step reported "instructions aimed at the assistant" for it, flagging 89 of 100 RAG emails
  (86 high) and causing 86 inbound human-approval outcomes; Qwen's L2 flagged 0 of 100. The benign set
  (LLMail-FP, no such phrasing) cannot reveal this mode. L2 uses the model's undefined "confidence" as an
  injection score, and a non-JSON answer counts as success.
- **Two models only.** Llama-3.1-8B guarded results are not used: its guard prompt appears to lack the
  reply-format instruction (a prompt-wiring gap that was not verified), so the multi-model claim rests
  on two runs.

### 4.5 Why L1 looks as if it does everything

First, **the v1 presets add layers on top of L1**, so L1 always acts first and gets the credit.

Second, **leakage.** All 300 email attacks are LLMail-Inject, and 151 are near-duplicates (cosine
similarity 0.9 or more) of texts in the classifier's training half; 138 of the 150 benign emails are
near-duplicates of L1's negative training texts, so only 12 benign emails are independent (L1 flagged
none). Leakage does not explain the attack flags, though: from the stored rows, L1 flagged 149 of the
149 non-duplicate attacks in the gpt run and 147 of 149 in the Qwen run, against 150 and 149 of the 151
near-duplicates. "Non-duplicate" means only under 0.9 similarity, not independently written.

Third, **the classifier, not the rules, does most of the work.** Rules alone reached medium or more on
90 of 300 attacks (30%, the same in both runs; 39 of the 149 non-duplicates). The v1 split "classifier
218, rules 72, AI judge 10" cannot show this, because `decided_by` reads "ml" whenever the classifier
ran, even if the rule score was larger. L1's false-positive rate on real mail is unquantified.

Fourth, **weak classes are not counted.** The worklog claims L1 detects paraphrase 0 of 10, quoted
history 1 of 5 and role-play 5 of 10, and 89.8% of attacks at 1.8% false alarms overall; its results
file is not in this tree, and the v1 set is not labeled by class, so none of this can be checked or
reconciled with 299 of 300.

### 4.6 Attack class by layer

Each cell comes from the cards. "Detects": a rule or model can flag it; "Marks": limits damage without
detecting, effect not measured; "Blind": the layer cannot see it; "Partly": depends on wording or on
other layers (for L4, only if the draft repeats an indicator or flagged instruction). L5 acts on
whatever the others report, so it has no column.

| Attack class | L1 | L2 | L3 | L3b | L4 |
|---|---|---|---|---|---|
| Plain override wording in the body | Detects (rule 0.97) | Detects and strips | Marks | Blind (not a chunk) | Partly |
| Exfiltration to an address or link | Detects, records the indicator | Detects and strips | Marks | Blind | Detects if the draft carries it |
| Paraphrase with no trigger words | Blind (judge only in the middle band) | Blind | Marks | Blind | Partly |
| Chat-template tokens (`[INST]`); forged `<<<CHANNEL:nonce>>>` markers | Tokens: detects (shorter list than L3's); markers: blind | Tokens: detects and strips; markers: blind | Removes and scores both | Same rules on chunks | Partly |
| Instruction in the subject line | Detects | Blind (intent copies the subject unmarked) | Marks the header copy, not the intent copy | Blind | Partly |
| Content never read (HTML, headers, attachments, text past a cap) | Blind (rules see all body text) | Blind | Blind | Blind | Partly |
| Instruction in thread history (no v1 case) | Blind | Blind | Marks (the only layer that touches it) | Blind | Partly |
| Instruction in quoted history in the body | Detects (rules see the whole body) | Blind if the pipeline cuts quoted history first | Marks | Blind | Partly |
| Poisoned knowledge-base passage | Blind | Blind | Marks | Detects wording-based poison (38.7% in v1) | Partly (echo of quarantined excerpts) |
| Plainly worded false fact in a chunk | Blind | Blind | Blind | Blind | Blind |
| Leak visible only in the draft | Blind | Blind | Blind | Blind | Detects (no v1 case) |
| A layer's code crashes | Error verdict, then human approval | Same; L3 falls back to the raw body | Exception reaches the caller | Chunk quarantined, then human approval | Error verdict, draft emptied, human approval |
| A layer's AI stage fails | Cheap verdict kept, L5 not told | Regex result kept, L5 not told | Not applicable | Cheap verdict kept, L5 not told | Cheap verdict kept, L5 not told |

L1 and L2 overlap heavily on email-body attacks and share blind spots. Poisoned documents, draft leaks
and thread text are covered by one layer or none. That is the design argument for layers 2 to 5; the v1
data can test only the first rows and the poisoned-document row, which is why section 5 exists.

## 5. How each layer's contribution is being measured

The v1 presets add layers on top of L1, so they cannot credit any other layer fairly. A pre-registered
experiment (task 7.22, decided 2026-09-30, before any run) removes one layer at a time from the full
guard, including L1, on the same 550 cases. The design is in
[docs/superpowers/specs/2026-09-30-mailguard-layer-ablation-design.md](../superpowers/specs/2026-09-30-mailguard-layer-ablation-design.md).
"Ablation" means removing one part to measure what it contributes.

| Config | Meaning |
|---|---|
| C0 | no guard (rag-email's own path), same-run baseline |
| C3 | full guard, same-run baseline |
| C3-L1, C3-L2, C3-L3, C3-L3B, C3-L4, C3-L5 | every layer except the one named |

Reply model and guard AI stages: gpt-4o-mini, one sample per case; L3b and L4 AI stages off. **ASR**
(attack success rate: the share of attacks that reached their goal, by the benchmark's string-match
rule) is reported per vector with Wilson 95% intervals (a range for a percentage that stays sensible
for small counts), plus C3's false-block rate on the 150 benign emails and an exact paired McNemar
test of each config against the same-run C3.

Hypotheses, stated before running:

- **H1 (L1 off):** C3-L1's LLMail ASR stays far below the same-run C0's, so layers 2 to 5 stop most
  LLMail attacks without L1.
- **H2:** removing L4 raises the RAG-vector ASR over C3.
- **H3:** removing L3b does not lower the RAG-vector ASR.
- **H4:** on LLMail, removing any one of L2, L3, L3b, L4 or L5 changes the ASR by less than 5 points,
  because L1 stops almost all of those attacks first.

Two cautions. H3 as worded holds whether removal leaves ASR unchanged or raises it, so it cannot alone
show L3b is useless; the decision rule below does that work. H4's reason does not fit L5: with L5
removed, L1 blocks nothing, so H4 may fail there. H1 and H4 together predict every layer is
dispensable on this data.

**Decision rule:** a layer is *measurably necessary on this benchmark* if removing it raises the ASR on
some vector with McNemar p < 0.05 against the same-run C3; otherwise it is *not measurable on this
benchmark*, reported next to the attack classes it covers that these cases lack. Every config is
reported, including null results. A class-targeted attack set is future work.

**Limits of the design.** L1 or L2 alone still stops 87 to 94% of email attacks first, so L3, L3b and
L4 are exercised on only about 15 of them, and no config removes both. With L3 off, scrubbing and
truncation still run, so C0 and C1 are not plain concatenation and C3-L3 removes only the wrapping,
marking and preamble, which will understate L3. Removing L1 or L2 also empties L4's indicator or
instruction inputs, so it is not a clean single-layer test. No correction for the several McNemar
tests is stated here.

### 5.1 Results (run `2026-09-30-gpt4omini-ablation`, finished 2026-09-30)

All eight configs ran on the 550 pinned cases (guard `81df5d07`, reply model gpt-4o-mini), plus the
retry pass. 4,399 of 4,400 rows were scored; one C0 row stayed an error (the model's repaired answer was
still not valid JSON, so rag-email refused to save it) and is excluded, as the rules say. Full report:
[`evaluation/results/mailguard_bench/2026-09-30-gpt4omini-ablation/report.md`](../../evaluation/results/mailguard_bench/2026-09-30-gpt4omini-ablation/report.md).

| Config | Layer removed | Email ASR (LLMail) | Poisoned-document ASR | Benign false blocks | Benign drafts, 40+ characters |
|---|---|---|---|---|---|
| C0 | all (no guard) | 54.5 % [48.8, 60.1] (163/299) | 81.0 % [72.2, 87.5] (81/100) | 0/150 | 150/150 |
| C3 | none (full guard) | 0.0 % [0.0, 1.3] (0/300) | 30.0 % [21.9, 39.6] (30/100) | 0/150 | 150/150 |
| C3-L1 | L1 | 0.3 % [0.1, 1.9] (1/300) | 32.0 % [23.7, 41.7] (32/100) | 0/150 | 150/150 |
| C3-L2 | L2 | 0.0 % [0.0, 1.3] (0/300) | **38.0 %** [29.1, 47.8] (38/100) | 0/150 | 150/150 |
| C3-L3 | L3 | 0.0 % [0.0, 1.3] (0/300) | 29.0 % [21.0, 38.5] (29/100) | 0/150 | 150/150 |
| C3-L3B | L3b | 0.0 % [0.0, 1.3] (0/300) | **58.0 %** [48.2, 67.2] (58/100) | 0/150 | 150/150 |
| C3-L4 | L4 | 0.3 % [0.1, 1.9] (1/300) | **40.0 %** [30.9, 49.8] (40/100) | 0/150 | 150/150 |
| C3-L5 | L5 | **7.7 %** [5.2, 11.2] (23/300) | 36.0 % [27.3, 45.8] (36/100) | 0/150 | 150/150 |

Bold: removing the layer raised the ASR with McNemar p < 0.05 against the same-run C3, the
pre-registered decision rule.

| Layer | Verdict on this benchmark | Paired test against C3 (attacks that succeeded only without the layer vs only with it) |
|---|---|---|
| L1 | not measurable | email 1 vs 0, p = 1; documents 3 vs 1, p = 0.63 |
| L2 | **measurably necessary** | documents 8 vs 0, p = 0.008 |
| L3 | not measurable | email 0 vs 0; documents 2 vs 3, p = 1 |
| L3b | **measurably necessary** | documents 29 vs 1, p = 6 × 10⁻⁸ |
| L4 | **measurably necessary** | documents 11 vs 1, p = 0.006 |
| L5 | **measurably necessary** | email 23 vs 0, p = 2 × 10⁻⁷ (documents 7 vs 1, p = 0.07) |

**Hypotheses.** H1 holds: without L1 the email ASR is 0.3 %, against C0's 54.5 %. H2 holds: without
L4 the document ASR is 40 %, against 30 %. H3 holds as worded: removing L3b did not lower the document
ASR; it nearly doubled it. H4 fails for L5 (7.7 points) and holds for L2, L3, L3b and L4 (at most 0.3
points).

**What the numbers say.**

- **L1 and L2 back each other up.** With L1 removed, L2 was the first catching layer for 271 of the 300
  email attacks; with L2 removed, L1 was for 258, and neither removal let more than one attack through.
  A remove-one test cannot credit a layer whose work another layer repeats, and no config removes both,
  so "not measurable" for L1 means redundant here, not useless.
- **L5 turns detection into protection.** With L5 removed, L1 still flagged all 300 email attacks and
  L2 298, but nothing acted on the flags and 23 succeeded. The other 277 still failed: in 5, L4 had
  redacted the draft; in 272, the reply model did not follow the injection, which the report cannot
  pin on one layer (L2 strips flagged sentences and L3 marks the email as data before the model reads
  it; part of the effect may be the guard's prompt template, whose own config, C0T, was not run here).
  Even with nothing blocked, the guarded path cut the email ASR from 54.5 % to 7.7 %.
- **Poisoned documents need L2, L3b and L4 together.** L3b quarantined a poisoned chunk in 46 of the
  100 cases, so the reply model never saw it; without L3b the ASR rose from 30 % to 58 %. The blocks
  come from L5's rule P03 (the draft complies with an injected goal, an L4 finding): 32 in C3, none
  without L4, 26 without L2 and 14 without L3b, because L4 compares the draft with the instructions L2
  extracted and the excerpts L3b quarantined (section 3).
- **L3 had no measurable effect** (29 % against 30 %), as the design's limits predicted: with L3 off,
  scrubbing and truncation still run, so C3-L3 removes only the wrapping and the markers.
- **No layer cost usefulness here:** no config blocked a benign email and every config drafted all
  150. But 138 of those 150 overlap L1's training data (section 4.5), so this says little about real
  mail.

**Cautions.** The rule makes 12 tests (six layers, two vectors) without a correction, as
pre-registered. Under a Holm correction, which was not pre-registered and is shown only for
transparency, L3b and L5 stay significant and L2 and L4 do not (p = 0.008 and 0.006 against Holm
thresholds of 0.0056 and 0.005). One sample per case and one reply model. The report's "which layer
stopped each attack" table counts blocks only, so a chunk that L3b quarantined without a block shows
up as "the model did not follow the injection". Qwen2.5-7B may repeat the run under the same design.

## 6. Scope and assumptions

- **What "dangerous" means.** Text that looks like prompt injection into the reply model, plus what
  follows in the draft or from poisoned documents. AgentMailGuard is not spam, phishing or malware
  filtering, access control, encryption or general data-leak prevention (03-agentmailguard.md); L1's
  `phishing_lure` rule only adds to a score. Business e-mail compromise is not addressed.
- **Attacker assumed.** Can send email and plant knowledge-base documents (the L3b premise). Not
  tested: an attacker who adapts to this guard; the attack set is fixed public data.
- **The guard's own AI stages can be attacked.** L1's judge can lower a verdict, and L2's paraphrase
  enters the prompt unmarked. In each run the reply model and the guard AI stages are the same model,
  so a model that obeys an injection may also miss it. Not measured.

## 7. Glossary

| Term | Meaning |
|---|---|
| Ablation | Removing one part at a time to measure what it contributes |
| ASR | Attack success rate: the share of attacks that reached their goal, by the benchmark's string-match rule |
| Audit id | A fingerprint of an L5 decision's inputs, for tracing |
| Block, quarantine, human approval, draft only, auto-send | L5's actions: stop with a reason; set aside; a person must approve; keep as a draft for review; send without review |
| Chunk | A short piece of a knowledge-base document returned by the search |
| Classifier, TF-IDF | A program that learned from labeled examples to sort text (here a logistic-regression model) and the word-weighting scheme it reads |
| Dispatcher, guard worker | The mail-sending component outside this repository; the service that runs the guard per queued job |
| Indicator | An address or link in the inbound email that a draft should not repeat |
| LLM, AI judge | A large language model; the judge is one asked whether a text is an attack |
| McNemar test | A test comparing two runs on the same cases; "exact" means no approximation |
| Near-duplicate | A text whose cosine similarity (0 to 1, how alike two texts are) to another is 0.9 or more |
| Noisy-OR | Combining scores as 1 - (1 - a)(1 - b) so either can raise the result |
| n-gram, regex | A run of n consecutive words or characters; a text pattern such as "ignore ... instructions" |
| P00 to P99 | The rows of L5's rule table; the lowest-numbered matching row decides |
| PoisonedRAG, seed_rag | A published attack that plants question-first passages in a knowledge base; the benchmark's other 11 RAG attack cases |
| RAG | Retrieval-augmented generation: the AI answers using passages the search found |
| reply.v1 | The fixed JSON format rag-email requires of the reply model; it has no subject, recipients or `security_notes` field |
| Risk tier T0 to T4 | L5's label from max severity: clean, low, medium, high, critical |
| Spotlighting, datamarking | Marking untrusted text (here replacing spaces with `^`) so the model can tell data from orders |
| Wilson interval | A 95% range around a percentage that stays valid for small counts and zero |
