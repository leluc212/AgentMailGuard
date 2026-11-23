# Paper outline — AgentMailGuard: Multi-Layer Prompt Injection Defense for LLM Agents Integrated with Email and RAG

Target: conference/journal paper (IEEE format, `paper/KLTN.pdf` is the current draft). This
file maps every section of the draft to the code and data that back it, and lists what is
still missing before submission.

## I. Introduction / II. Research questions
* RQ1 ASR reduction, RQ2 utility (TSR >= 80 %), RQ3 per-layer contribution. -> `docs/experiments.md`.

## III. Related work
Already covers Perez & Ribeiro, Greshake, Liu, InjecAgent, ASB, AgentDojo, StruQ,
Spotlighting, Task Shield, DRIFT, adaptive attacks, PoisonedRAG, RobustRAG, NeMo Guardrails.
Add: LLMail-Inject (Abdelnabi et al., 2025) as the closest email benchmark; CaMeL / "Defeating
prompt injections by design" for capability-based isolation; Prompt Guard 2 as an L1 baseline.

## IV. Threat model -> `docs/architecture.md` (threat model section), `evaluation/attacks/taxonomy.py`.

## V. Architecture
Guard v3 (ADR-0015): four layers named by meaning plus channel isolation, which builds the prompt and is not a
layer. The subsection letters below follow the v3 layer order; the current draft (`paper/KLTN.pdf`) still uses the
v2 six-layer numbering (Layer 1, 2, 3, 3b, 4, 5) and has to be renamed to match.

| Paper subsection | Implementation |
|---|---|
| V-B L1 Inbound Guard | `mailguard/layers/inbound_guard/{guard,scan,extract,rules,classifier,llm_judge}.py`: the scan sub-stage (rules, classifier, judge) and the extract sub-stage (strip, intent) in one verdict |
| V-C L2 Doc Scanner | `mailguard/layers/doc_scanner/{scanner,chunk_scanner,signals,set_judge}.py` |
| V-D L3 Inbound Gate | `mailguard/layers/inbound_gate/gate.py`, `mailguard/policy/engine.py`, `configs/policy.yaml` (version `2026.10-v3`) |
| V-E L4 Output Guard | `mailguard/layers/output_guard/{guard,scanner}.py`, `configs/pii_patterns.yaml` (outbound policy and the inbound floor in `guard.py`) |
| V-F Channel isolation (a switch, not a layer) | `mailguard/prompting/channel_isolation.py`, `configs/channels.yaml` (spotlighting modes `delimit`, `datamark`, `encode`) |
| Prompts | `mailguard/prompts/{inbound_scan_judge,inbound_extractor,doc_chunk_judge,doc_set_judge,output_judge}.v1.txt`; the benchmark's guarded reply prompt is `guarded.v3` (`evaluation/mailguard_bench/guarded_reply.py` in the host repository) |
| Fig. 1 | `AgentMailGuard_Security_Architecture.html` (Archify; still drawn with the v2 layers) |

Suggested additions to the text: the cascade/uncertain-band rule and the noisy-OR fusion (V-B), the question-sentence
echo and ordered-prefix signals, bait clusters and the dev-split threshold selection (V-C), the strictest-action rule
table and the two gate passes (V-D), the injected-goal compliance check and the inbound floor (V-E), the nonce-marker
and datamark equations (V-F). Report schema 3, fail-closed judges (`judge_invalid:<stage>` leads to human review) and
the guard call budget of five per job belong in the design paragraph.

## VI. Experimental setup
* VI-A Datasets -> `docs/datasets.md`, `mailguard/datasets/sources.py`; add LLMail-Inject and
  the benign corpora (Enron ham, Bitext) to the list; report counts from
  `datasets/processed/email_bench/stats.json` and `l1_injection/stats.json`.
* VI-B Models -> `configs/models.yaml`; note Q4_K_M quantization and BGE-M3 is used by the
  core, not by the guard (the guard's ML stage is TF-IDF/LR).
* VI-C Metrics -> `evaluation/metrics.py` (define ASR strictly on the final artifact; mention
  `goal_achieved_unreviewed` as the optimistic variant).
* VI-D Configurations -> `mailguard/pipeline.py::GuardConfig.preset`.

## VII. Results
* Table V from `paper/tables/table_v_security_utility.tex` (per model x config).
* Table VI from `paper/tables/table_vi_ablation.tex` (McNemar p-values).
* The tables committed in `paper/tables/` are **v2 results (scheme v2, pre ADR-0015)**: naive-simulator runs of the six-layer guard, with the configs `C3-L3b` and `C3-L5` that no longer exist. They are kept for reference and must not be quoted as v3 results. Task 16 (plan_v3, the live v3 run) fills the v3 tables; no v3 number is invented here.
* Add a per-technique breakdown (`asr_by_technique.md`) and the L1 classifier card
  (`artifacts/models/l1_injection_clf_v1.metrics.json`: F1 0.946, AUROC 0.990, FPR 2.0 %, the classifier is unchanged in v3).
* Add latency: guard-only mean/p95 from `summary.json`.

## VIII. Conclusion / limitations to state honestly
* Knowledge-corruption poisoning without instruction phrasing (PoisonedRAG factual passages)
  is only partially detectable by the L2 Doc Scanner's heuristics (the dev split shows 0 % "every chunk caught" for paraphrased echo without a prefix); report it as a limitation and future work
  (RobustRAG-style voting, citation grounding checks).
* Adaptive attacks (paraphrase, multilingual) are included in the seed templates; a proper
  adaptive-attack evaluation following Zhan et al. (NAACL'25) is future work.
* LLM judges are 7B/8B quantized; results depend on the judge prompt versions (`inbound_scan_judge.v1`, `inbound_extractor.v1`, `doc_chunk_judge.v1`, `doc_set_judge.v1`, `output_judge.v1`) and on the benchmark's guarded reply prompt `guarded.v3`.

## Remaining work before submission
1. Run the three models on the live v3 benchmark (plan_v3 Task 16; `docs/experiments.md`, section "The live benchmark") and fill the v3 Tables V/VI; until then the committed tables are labelled v2 results.
2. Optional: LoRA-fine-tuned judges (`training/`) as an extra row.
3. Human review of 100 sampled drafts for TSR validation (report agreement with keyword TSR).
4. Replace `[Student N]` placeholders and add acknowledgements/dataset licenses.
