# AgentMailGuard layer by layer: what each check reads, decides and hands on

**Who this is for:** thesis reviewers and technical readers new to the project. Terms are defined at
first use and again in the glossary (section 7). Background on the whole guard is in
[03-agentmailguard.md](03-agentmailguard.md). This document describes the **v3 guard** (ADR-0015,
[docs/adr/0015-guard-architecture-v3.md](../adr/0015-guard-architecture-v3.md)): four layers plus one
prompt-building switch. Sections 4 and 5 are results measured on the earlier six-layer guard (v1) and
are kept, labelled, as history; the v3 numbers come from the v3 benchmark run (plan_v3 Task 16) and
do not exist yet.

## 1. The two questions and the short answers

1. **For each AgentMailGuard layer: what goes in, how does it tell dangerous from harmless, and what
   comes out?** Section 3 gives a short card per layer; the full cards are in
   [layers/](layers/). Section 2 shows how the layers hand data to each other.
2. **Why are layers 2 to 4 needed when layer 1 stops most attacks in the v1 results?** Three answers.
   - **L1 alone stops nothing.** It only returns a verdict; the **L3 Inbound Gate** turns verdicts
     into an action. "L1 stops most attacks" therefore means L1 plus the gate (preset C1).
   - **Whole attack classes are outside L1's input.** It never sees retrieved knowledge or the draft.
     Only the L2 Doc Scanner and the L4 Output Guard touch those, and channel isolation is the only
     protection thread text has.
   - **v1 measured that the layers back each other up** (section 5.1, old layer names): removing the
     document scanner, the output scanner or the policy engine raised the attack success rate, and
     removing L1 changed almost nothing because the extractor caught the same email attacks. v3 changes
     the layer layout (the extractor became L1's second sub-stage and the document scanner now runs
     before the gate), so these figures are not v3 results; the v3 acceptance criteria are in ADR-0015
     decision 13.

Terms first. **AgentMailGuard** is the security package around rag-email's reply writer. A **prompt
injection** is text in an email or document that tries to give orders to the AI that writes the reply
(the **reply model**, a large language model or **LLM**). A **layer** is one stage of the guard that
returns a **verdict**: a **severity** (none, low, medium, high, critical, from a **score** between 0 and
1) and **findings**. The four layers are **L1 Inbound Guard**, **L2 Doc Scanner**, **L3 Inbound Gate**
and **L4 Output Guard**. **Channel isolation** builds the prompt and decides nothing, so it is a
switch, not a layer. The **policy engine** is shared infrastructure used by L3 (stage inbound) and L4
(stage outbound); it is not a layer. The benchmark presets (**configs**) choose layers: C0 none, C0T
the guard's prompt template only (a benchmark baseline), C1 = L1 + L3, C2 = C1 + L2 + isolation,
C3 all, C3-L1 to C3-L4 each minus one layer, C3-ISO minus isolation. "Flagged" means severity medium
or higher. "Not determined" marks something not verified.

## 2. The guard at a glance

One email travels down the page, in the order the checks run.

```
 inbound email (subject, body, sender)
        |
        v
 +--------------------------------+
 | L1 Inbound Guard               |  scan: rules, n-gram model, optional AI judge (severity comes from here)
 |   scan + extract sub-stages    |  extract: strips instruction-like sentences, neutral intent
 +--------------------------------+
        | one verdict ----------------------------> L3 pass 1
        | (a) indicators + injected instructions -> held for L4
        | (b) sanitized_body, intent, question sentences -> held for isolation and L2
        v
 +--------------------------------+
 | L3 Inbound Gate, pass 1        |-----> block or quarantine: STOP (escalated draft, no model call,
 +--------------------------------+       the Doc Scanner never runs)
        | any other decision
        v
 knowledge-base search --> retrieved chunks (none for a template job: L2 is a no-op)
        |
        v
 +--------------------------------+
 | L2 Doc Scanner                 |  per chunk: rules, heuristics, classifier, echo of the email's questions,
 +--------------------------------+  bait clusters, optional chunk and set judges; trust per chunk
        | verdict, kept chunks
        v
 +--------------------------------+
 | L3 Inbound Gate, pass 2        |-----> block or quarantine: STOP (escalated draft, no model call)
 +--------------------------------+       (the final inbound decision; the only one audited)
        | any other decision
        v
 +--------------------------------+
 | channel isolation (not a layer)|  scrubs, caps, marks, builds the prompt from the kept chunks
 +--------------------------------+
        | marked prompt
        v
    reply model ----> draft
        |
        v
 +--------------------------------+
 | L4 Output Guard                |  scans the draft against (a), redacts secrets and personal data,
 |   scan + outbound policy +     |  decides with the outbound policy, then merges the inbound decision
 |   inbound floor                |  as a floor (strictest wins)
 +--------------------------------+
        | final decision: quarantine / block / human approval / draft only / auto-send
```

- **Indicators** (a) are every e-mail address and link found in the email, the sender's own included.
  L4 checks whether the draft repeats one, or any "injected instruction" from L1 or from quarantined chunks.
- The **gate decides twice before generation** (pass 1 on L1's facts, pass 2 on every fact) and **L4
  decides once after generation**; there is no separate policy step. Exit points of a job:
  `gate_pass_1`, `gate_pass_2`, `output_guard`, `delivered`. The **dispatcher** (the mail-sending
  component outside this repository) sends only when the final decision is auto-send.
- A judge that fails or answers invalidly **fails closed** by default (`judge_invalid:<stage>` leads to
  human review); a judge that is not configured degrades silently. At most five guard model calls per
  job (production default two). Reports carry `schema_version: 3`.

## 3. The layers, one by one

Severity from score: under 0.20 none, 0.20 low, 0.50 medium, 0.85 high, 0.97 critical (L1's bands; the
Doc Scanner's chunk ladder starts high at 0.70). **Fail-closed** means a layer that crashes reports a
HIGH error verdict instead of a clean one; the policy rule P00 then asks for human review, and
because the strictest matching action wins it no longer hides a stricter quarantine or block.

Scores are hand-set constants and each layer combines them differently (L1 noisy-OR, the extract
sub-stage maximum, Doc Scanner noisy-OR), yet the policy compares severities across layers as if equal.

`GuardedEmail` is the record holding one email. **Quoted history** is earlier messages pasted inside
the body; **thread history** is earlier messages passed to the prompt builder as separate fields.

### L1 - Inbound Guard

Reads one email and returns one verdict. **scan** sub-stage: 15 rule families plus obfuscation
heuristics, then a calibrated n-gram classifier (skipped when a rule is already sure), then an optional
AI judge for the uncertain band. **extract** sub-stage: strips instruction-like segments, builds a
sanitized body and a neutral intent, with an optional AI paraphrase. Severity and score come from the
scan only; the extractor adds findings and facts but never raises severity (ADR-0015 decision 9). At
most two model calls. Hands on: the verdict to the gate, the sanitized body and intent to isolation, the
question sentences to L2, indicators to L4. Full card: [layers/L1-inbound-guard.md](layers/L1-inbound-guard.md)

### L2 - Doc Scanner

Reads every retrieved knowledge chunk before the prompt is built and before the gate's final pass.
Cheap stage per chunk (rules, poison heuristics, classifier), then signals against the email's question
sentences (`echo_vs_question`, `ordered_prefix`), bait clusters, an optional per-chunk judge for
uncertain chunks and an optional set judge (adds quarantines only). Each chunk gets a trust level
(trusted, untrusted, quarantined). Skipped when pass 1 stops the job or when there are no chunks. Full
card: [layers/L2-doc-scanner.md](layers/L2-doc-scanner.md)

### L3 - Inbound Gate

Runs the shared policy engine at stage inbound, twice: pass 1 on L1's facts (a stop here skips the
Doc Scanner), pass 2 on every fact. Strictest matching rule wins; only the final pass is audited. Block
or quarantine stops the job before any model call. Full card:
[layers/L3-inbound-gate.md](layers/L3-inbound-gate.md)

### Channel isolation (not a layer)

Builds the prompt: scrubs forged markers and chat-template tokens from untrusted text, caps its size,
marks it as data (spotlighting) and renders each kept chunk with its trust. It returns the prompt and an
`IsolationInfo` record, never a verdict; markers forged in thread history are recorded, not graded.
Switch: on in C2 and C3, off in C0, C1 and C3-ISO. Full card:
[layers/channel-isolation.md](layers/channel-isolation.md)

### L4 - Output Guard

Reads the finished draft: sensitive-data patterns (redacted), protected-text leaks, citation integrity,
injected-goal compliance, unsafe actions, external links, optional AI judge. Then it decides with the
outbound policy and merges the inbound decision as a floor (a specific, stricter-than-auto-send inbound
rule is never weakened). Full card: [layers/L4-output-guard.md](layers/L4-output-guard.md)

**Cost and speed.** No v3 measurement exists yet; the v3 run reports guard calls and p50/p95 latency per
configuration (Task 16). The v1 figures in section 4 and 5 describe the old guard.


## Part II. v1 results (scheme v1, pre-v3 guard)

**Everything in sections 4 and 5 was measured on the six-layer v1 guard (guard commit 81df5d07) and uses
its layer names.** Mapping to v3: v1 L1 scanner and L2 extractor = the two sub-stages of the v3 L1 Inbound
Guard; v1 L3b document scanner = the v3 L2 Doc Scanner (which now also runs before the gate); v1 L5 =
the shared policy engine, used by the v3 L3 Inbound Gate (inbound) and L4 Output Guard (outbound); v1 L3
channel isolation = the v3 channel isolation switch (ablation C3-ISO); v1 L4 = the v3 L4 Output Guard.
These results are not v3 results and are not comparable with them; the committed run folders are
re-read with `make mailguard-report RUN=<id> LEGACY=1`.

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
- **Attacker assumed.** Can send email and plant knowledge-base documents (the Doc Scanner premise).
  Not tested: an attacker who adapts to this guard; the attack set is fixed public data.
- **The guard's own AI stages can be attacked.** L1's judge can lower a verdict, and the extractor's
  paraphrase enters the prompt unmarked as a semi-trusted fact; the set judge's reason text is model
  output and untrusted. In a benchmark run the reply model and the guard AI stages are the same model,
  so a model that obeys an injection may also miss it. Not measured.
- **Host hooks are out of scope here.** The host-side consumers of chunk `trust` are the separate
  ADR-0016 and are not built.

## 7. Glossary

| Term | Meaning |
|---|---|
| Ablation | Removing one part at a time to measure what it contributes |
| ASR | Attack success rate: the share of attacks that reached their goal, by the benchmark's string-match rule |
| Audit id | A fingerprint of a gate decision's inputs, for tracing |
| Block, quarantine, human approval, draft only, auto-send | The policy engine's actions: stop with a reason; set aside; a person must approve; keep as a draft for review; send without review |
| Chunk | A short piece of a knowledge-base document returned by the search |
| Classifier, TF-IDF | A program that learned from labeled examples to sort text (here a logistic-regression model) and the word-weighting scheme it reads |
| Dispatcher, guard worker | The mail-sending component outside this repository; the service that runs the guard per queued job |
| Indicator | An address or link in the inbound email that a draft should not repeat |
| LLM, AI judge | A large language model; the judge is one asked whether a text is an attack |
| McNemar test | A test comparing two runs on the same cases; "exact" means no approximation |
| Near-duplicate | A text whose cosine similarity (0 to 1, how alike two texts are) to another is 0.9 or more |
| Noisy-OR | Combining scores as 1 - (1 - a)(1 - b) so either can raise the result |
| n-gram, regex | A run of n consecutive words or characters; a text pattern such as "ignore ... instructions" |
| P00 to P99 | The rows of the policy rule table; every matching row is collected and the strictest action wins |
| PoisonedRAG, seed_rag | A published attack that plants question-first passages in a knowledge base; the benchmark's other 11 RAG attack cases |
| RAG | Retrieval-augmented generation: the AI answers using passages the search found |
| reply.v1 | The fixed JSON format rag-email requires of the reply model; it has no subject, recipients or `security_notes` field |
| Risk tier T0 to T4 | The policy's label from max severity: clean, low, medium, high, critical |
| Spotlighting, datamarking | Marking untrusted text (here replacing spaces with `^`) so the model can tell data from orders |
| Wilson interval | A 95% range around a percentage that stays valid for small counts and zero |
| Inbound floor | The inbound gate's decision carried into L4's final decision so that the strictest action wins across both |
| Gate pass | One evaluation of the inbound policy: pass 1 on L1's facts, pass 2 on every fact |
| Trust (of a chunk) | The Doc Scanner's label: trusted, untrusted or quarantined |
