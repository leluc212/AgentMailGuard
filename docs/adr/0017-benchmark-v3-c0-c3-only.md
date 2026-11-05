# ADR-0017: the v3 benchmark runs C0 and C3 only

- **Status:** Accepted, 2026-11-05
- **Date:** 2026-11-05
- **Decided by:** project owner (plan review of 2026-11-05, `implementation_plan.md`)
- **Number:** 0017, because ADR-0015 decision 11 reserves ADR-0016 for the host hooks of task 7.32
- **Task:** `specs/tasks.md` 7.33
- **Amends:** ADR-0015 (the scheme v3 config list), plan_v3 Task 16b (the default campaign and
  acceptance criterion (e), the ablations), `specs/tasks.md` 7.19 (C0T as a required baseline)
  and 7.31 (its config list and its ablation criterion)
- **Changes:** root `evaluation/mailguard_bench` (`scheme.py`, `report.py`, `artifacts.py`,
  `scheme_report.py`, `meaning.py`, `analyses.py`, docstrings), `Makefile`, `tests/unit`, docs,
  `specs/`. **Not** `agentmailguard/`.

## Context

Scheme v3 (ADR-0015) had ten configs: the baselines `C0` (rag-email alone) and `C0T` (the guard's
prompt template with no layer), the guard presets `C1` and `C2`, the full guard `C3`, and five
"C3 minus one part" ablations (`C3-L1` to `C3-L4`, `C3-ISO`). The default campaign ran nine of
them (all but `C0T`): 9 x 1,800 = 16,200 case runs per model.

The owner wants the next benchmark runs to compare the full guard with no guard, and nothing else.
Choosing `CONFIGS=C0,C3` per run works, but leaves the other names runnable and the default at nine
configs, so a run that forgets the argument costs about 4.5 times as much.

The guard subtree is pinned by its tree hash (`MAILGUARD_V3_TREE` in the `Makefile`,
`guard_env.V3_MAILGUARD_TREE`): `live.run`, `live.guard_worker`, `runner`, the kit, `guard_smoke` and
`build_cases` refuse a guard directory whose tree differs from the pin. Editing
`agentmailguard/mailguard/pipeline.py` to drop the presets would stop every benchmark run until the
guard is re-pinned, and new runs would then record a different `mailguard_commit` than the
published v3 runs.

## Decision

1. **Scheme v3 has two configs: `C0` and `C3`.** `scheme.V3_LAYERS` holds only these two;
   `V3_CONFIGS`, `V3_AI_STAGES` and the default campaign (`V3_DEFAULT_CAMPAIGN`, the kit's
   `DEFAULT_CONFIGS`) follow from it. `V3_ABLATIONS` is empty. `C0T`, `C1`, `C2`, `C3-L1` to `C3-L4`
   and `C3-ISO` are refused by every v3 entry point (`live.run`, `live.guard_worker`, `runner`,
   `make mailguard-bench`), naming the two configs. A default run is 2 x 1,800 = 3,600 case runs.
2. **The guard is not changed.** `GuardConfig.preset` in `agentmailguard/` keeps all its presets, and
   the pin stays `d95e542`. The benchmark simply never builds the other presets. The meanings of `C0`
   and `C3` (`V3_LAYERS["C0"] = ()`, `V3_LAYERS["C3"]` = every switch, every judge of C3 live) do
   not change.
3. **One baseline.** The report pairs `C0` against `C3` (McNemar) and no longer prints a `C0T` line.
4. **No silent drop of old raw files.** A v3 run folder whose `raw/` holds the `<config>.jsonl` of
   a config this decision removed (`scheme.V3_REMOVED_CONFIGS`; a restored `bench-results-<id>.zip`
   of an earlier run) is refused by the report, the meaning reader and the analyses, naming the
   files and the commit to rebuild it from (`scheme.V3_FULL_CAMPAIGN_COMMIT`,
   `4fe2ec3f83a64908f2830c268d2371c28d193cbd`). Before this decision such a file was scored;
   ignoring it now would rewrite the report without its rows. Files of other names (a stray v1
   config file such as `C3-L5.jsonl`) are still left out, as before.
5. **The layer-ablation report code stays, unused.** `report.layer_ablation`,
   `artifacts._layer_ablation_section` and their dataclasses still run on an empty list and leave
   the section out. Re-enabling an ablation is adding it back to `V3_LAYERS` and `V3_ABLATIONS`.
6. **Schemes v1 and v2 are untouched.** Their config tables (`V1_CONFIGS`, `LAYER_ABLATIONS`,
   `V2_*`), the v1 runner tables and the legacy reader keep reading the committed v1/v2 folders.

## Consequences

- The report of a new run has no C0T vs C3 comparison, so it cannot separate what the guard's prompt
  template adds from what its layers add.
- New runs have no layer-ablation table: the contribution of each layer (L1 to L4, channel
  isolation) is not measured. `paper/figures/.../fig2_configurations` and `fig3_ablation` cannot be
  rebuilt from them; the committed figures and results are not changed.
- The committed v3 result folders (2026-10-17 to 2026-10-19) are static files and stay as they are.
  Rebuilding one of their reports from restored raw files needs a checkout of
  `V3_FULL_CAMPAIGN_COMMIT`.
- The guard library still offers `C1`, `C2` and the ablations to direct callers
  (`agentmailguard/scripts/scan_email.py --config`, `agentmailguard/evaluation/run_benchmark.py`).
- No requirement ID changes. R22.12 (versioned, reproducible benchmark artifacts) still holds: the
  scheme stays `v3` and every run records it.

## Re-enabling a config

Add it back to `scheme.V3_LAYERS` (an ablation also to `V3_ABLATIONS`; `C0T` also to
`report.BASELINES` and the baseline lines of `artifacts.render_report`), to the `mailguard-bench`
case list of the `Makefile`, and restore its tests from `V3_FULL_CAMPAIGN_COMMIT`. The guard needs
no change.
