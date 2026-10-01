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
| Paper subsection | Implementation |
|---|---|
| V-B Layer 1 | `mailguard/layers/l1_injection_scanner` (rules/classifier/llm_judge/scanner) |
| V-C Layer 2 | `mailguard/layers/l2_intent_extractor/extractor.py` |
| V-D Layer 3 | `mailguard/layers/l3_channel_isolation/isolation.py`, `configs/channels.yaml` |
| V-E Layer 3b | `mailguard/layers/l3b_document_scanner/scanner.py` |
| V-F Layer 4 | `mailguard/layers/l4_output_scanner/scanner.py`, `configs/pii_patterns.yaml` |
| V-G Layer 5 | `mailguard/layers/l5_policy_engine/engine.py`, `configs/policy.yaml` |
| Fig. 1 | `AgentMailGuard_Security_Architecture.html` (Archify) |

Suggested additions to the text: the cascade/uncertain-band rule (Sec. V-B), the noisy-OR
fusion formula, the nonce-marker and datamark equations (V-D), the quarantine ratio
escalation (V-E), the injected-goal compliance check (V-F), the priority-ordered rule table (V-G).

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
* Add a per-technique breakdown (`asr_by_technique.md`) and the L1 classifier card
  (`artifacts/models/l1_injection_clf_v1.metrics.json`: F1 0.93, AUROC 0.99, FPR 1.4 %).
* Add latency: guard-only mean/p95 from `summary.json`.

## VIII. Conclusion / limitations to state honestly
* Knowledge-corruption poisoning without instruction phrasing (PoisonedRAG factual passages)
  is only partially detectable by L3b heuristics; report it as a limitation and future work
  (RobustRAG-style voting, citation grounding checks).
* Adaptive attacks (paraphrase, multilingual) are included in the seed templates; a proper
  adaptive-attack evaluation following Zhan et al. (NAACL'25) is future work.
* LLM judges are 7B/8B quantized; results depend on prompt version `l1_judge.v1`.

## Remaining work before submission
1. Run the three models on the full benchmark (`docs/experiments.md`, step 3) and fill Tables V/VI.
2. Optional: LoRA-fine-tuned judges (`training/`) as an extra row.
3. Human review of 100 sampled drafts for TSR validation (report agreement with keyword TSR).
4. Replace `[Student N]` placeholders and add acknowledgements/dataset licenses.
