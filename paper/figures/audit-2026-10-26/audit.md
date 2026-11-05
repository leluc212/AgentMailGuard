# Three-model chart audit - 26 October 2026

## Conclusion

The original CSV percentages reproduce from their stated counts. The rendered summary table has minor interval-display discrepancies listed below. The principal corrections concern metric meaning, exposure denominators, omitted semantic scores, and overly broad interpretations. No experimental outcomes have been changed.

This is an aggregate-level audit of the 19 October v3 runs. Raw pipeline JSONL records, run-local cases.jsonl, and the original .agents/bench/make_figures_paper.py are absent. Full event-level re-scoring, freshness validation of semantic verdicts, and independent source attribution are therefore incomplete. The new source-breakdown figure is explicitly PROVISIONAL.

## Verification

- 108/108 original plotted percentages and Wilson intervals reproduce from their CSV counts.
- 90/90 non-source-breakdown plotted count pairs match summary.json exactly.
- All 30 original PDF summary rows have matching denominators and benign rates; 3 attack-rate/interval display strings differ from direct one-decimal recomputation (details below).
- All six source-breakdown totals reconcile to saved RAG totals; source assignments remain unverified.
- 240 selected metrics.csv rows agree with summary.json.
- 60 semantic pipeline rates independently recounted from latest saved meaning verdicts; draft freshness cannot be checked without raw files.
- All 30 run/config manifests share case hash, host commit, guard commit, L1 classifier hash and embedding settings.

## Corrections across the six original figures

| Figure | Finding | Replacement |
|---|---|---|
| 1 | 100 minus pipeline ASR credits triage as well as the guard. | Show pipeline ASR and drafting-stage ASR side by side; lower is better. |
| 2 | A generic defense-rate axis hides the conditional population; C0T is not an undefended prompt. | Explicit drafting-stage denominator; retain C0T as a template control. |
| 3 | Near-100% defense compresses failures and does not establish each layer's independent effect. | Plot attack-success rates with counts and intervals; avoid causal layer rankings. |
| 4 | Source totals match aggregate RAG counts, but source-specific raw records are absent. | Preserve source separation, convert to ASR, prominently mark provisional. |
| 5 | Generic FPR uses all benign messages; utility is a minimum-length proxy, not measured quality. | Separate pipeline blocking, exposed blocking, exposed escalation and draft proxy. |
| 6 | A summary without the above distinctions encourages incompatible comparisons. | Regenerate all 30 model/config rows with explicit event/eligible counts and error counts. |

S1 adds the pre-registered semantic-reader comparison. S2 separates RAG attacks where poisoned material was actually retrieved.

## Numerical examples for the defense

| Model | C3 email guard ASR | C3 RAG guard ASR | C3 benign escalation | C0 to C3 draft proxy |
|---|---|---|---|---|
| GPT-4o-mini | 0.00% (0/466) | 8.49% (9/106) | 4.10% (8/195) | 38.80% (194/500) to 37.60% (188/500) |
| Qwen2.5-7B | 0.00% (0/716) | 40.88% (121/296) | 8.30% (24/289) | 56.31% (281/499) to 56.40% (282/500) |
| Llama-3.1-8B | 0.00% (0/790) | 12.11% (35/289) | 6.33% (21/332) | 66.73% (333/499) to 55.91% (279/499) |

## Original summary-table display discrepancies

GPT-4o-mini C3-L1, email defense: rendered 89.6 [87.5, 91.3]; directly recomputed 89.6 [87.6, 91.3]
GPT-4o-mini C3-ISO, RAG defense: rendered 96.3 [93.5, 97.9]; directly recomputed 96.3 [93.6, 97.9]
Qwen2.5-7B C0, email defense: rendered 65.6 [62.5, 68.5]; directly recomputed 65.6 [62.6, 68.5]

These strings are checked against full-precision saved counts/intervals, not rounded metrics.csv values. The replacement calculates directly from counts.

## Missing-case sensitivity (not a confidence interval)

For pipeline ASR with k observed successes, n scored cases and N planned cases, the all-planned range is [k/N, (k+N-n)/N]. This treats unknown outcomes as all failures or all successes; neither bound is an observed rate. It must not be substituted for the guard denominator because missing cases have unknown drafting exposure.

| Model | C3 email scored/planned | Observed pipeline ASR | All-planned sensitivity range |
|---|---|---|---|
| GPT-4o-mini | 1000/1000 | 0.00% | 0.00% to 0.00% |
| Qwen2.5-7B | 984/1000 | 0.00% | 0.00% to 1.60% |
| Llama-3.1-8B | 999/1000 | 0.00% | 0.00% to 0.10% |

## Interpretation and limitations

- Zero observed successful attacks is not proof of zero future risk. Whiskers use Wilson 95% intervals and remain nonzero at zero events.
- Intervals are nominal case-level binomial intervals. Attack families, reused templates and related cases may violate independence; these are not cluster-adjusted or repeated-run uncertainty estimates.
- All three models occupy triage, generation and guard-judge roles. Exposure counts change, so comparisons concern whole model/route configurations, not only the reply model.
- The string rule can count an attacker address mentioned in a refusal; the semantic scorer estimates intended behavior and excludes unclear cases. Report both without presenting either as human-validated ground truth.
- The LLMail benchmark is adapted to final drafts. The original Microsoft task uses a specified tool/API call; these results are not directly the original challenge ASR.
- The reports disclose overlap between LLMail benign examples and L1 training negatives. The full benign rate may be optimistic; the Enron-only rate cannot be independently reconstructed from the available raw artifacts.
- The L1 classifier was retrained; report its manifest hash. The attacks were not adaptively optimized against this guard. Synthetic RAG attacks remain separate.
- No new model calls were made, no attack/detection logic was changed, and no operational stack or CI result is claimed.

## Suggested answers to lecturers

**Why did the old graph show a stronger defense?** It plotted the complement of pipeline ASR, including attacks stopped before the guard. The replacement shows pipeline and drafting-stage populations separately.

**Is the system 100% secure?** No. We observed zero successful email attacks in the scored C3 sample under this scorer; RAG still has successes, some runs have missing cases, and the confidence interval is not zero-width.

**Does a 40-character draft prove usefulness?** No. It is a pre-registered output proxy. Human correctness and acceptance evaluation would require separate evidence.

**Which model is best?** These runs show a security/benign-output trade-off under distinct model serving routes and triage behavior. They do not establish a universal winner.

## Research sources

- Microsoft LLMail-Inject analysis and original task definition: https://github.com/microsoft/llmail-inject-challenge-analysis
- NIST guidance on binomial proportions and boundary uncertainty: https://www.itl.nist.gov/div898/software/dataplot/refman2/auxillar/exacbici.htm
- Wilson implementation reference: https://support.sas.com/documentation/cdl/en/procstat/68142/HTML/default/procstat_freq_details37.htm
- Project contracts: ADR-0010, ADR-0011, ADR-0012, ADR-0015; specs/tasks.md 7.19/7.20; R22.12. Saved run reports supply the study-specific caveats.

## Reproduce

Install matplotlib==3.9.2 and pdfplumber in a Python environment, then run `python paper/figures/audit-2026-10-26/build.py` from the repository. The script also recognizes a workspace-local `.chart-deps` installation. All reads are offline. Original figure folders are preserved. Run test_audit.py for the five offline regression checks.

See figure_data.csv for every new plotted count and audit_manifest.json for SHA-256 hashes of all inputs. The summary table reads the same saved aggregates. PNG, vector PDF and editable SVG versions are provided.
