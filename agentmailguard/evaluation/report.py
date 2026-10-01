"""Turn benchmark result directories into the paper's tables (Markdown + LaTeX).

    python -m evaluation.report evaluation/results/<run> [more runs...] --out paper/tables

Produces
    table_v_security_utility.{md,tex}   ASR / TMR / DER / TSR / FPR / latency per (model, config)
    table_vi_ablation.{md,tex}          ASR(email) / ASR(rag) for C3 and each C3-minus-layer, with
                                        McNemar p-values against C3 (paired on case ids)
    asr_by_technique.md                 per-technique ASR for every config (diagnostics)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from evaluation.metrics import CaseResult, Proportion, paired_comparison, summarize

ABLATIONS = ["C3", "C3-L1", "C3-L2", "C3-L3", "C3-L3b", "C3-L4", "C3-L5"]
LAYER_LABEL = {
    "C3": "C3 Full (baseline for ablation)",
    "C3-L1": "C3 $-$ Layer 1 (no Email Scanner)",
    "C3-L2": "C3 $-$ Layer 2 (no Intent Extract)",
    "C3-L3": "C3 $-$ Layer 3 (no Channel Iso.)",
    "C3-L3b": "C3 $-$ Layer 3b (no Doc Scanner)",
    "C3-L4": "C3 $-$ Layer 4 (no Output Scan)",
    "C3-L5": "C3 $-$ Layer 5 (no Policy Engine)",
}


def load_run(run_dir: Path) -> dict[tuple[str, str], list[CaseResult]]:
    out: dict[tuple[str, str], list[CaseResult]] = {}
    for path in sorted(run_dir.glob("*__*.jsonl")):
        agent, config = path.stem.split("__", 1)
        with open(path, encoding="utf-8") as f:
            out[(agent, config)] = [
                CaseResult.from_dict(json.loads(line)) for line in f if line.strip()
            ]
    return out


def pct(p: Proportion) -> str:
    return f"{p.pct:.1f}"


def table_v(runs: dict[tuple[str, str], list[CaseResult]]) -> tuple[str, str]:
    rows = []
    for (agent, config), results in sorted(runs.items()):
        s = summarize(results)
        rows.append((agent, config, s))
    md = [
        "| Model | Config | ASR↓ | TMR↓ | DER↓ | TSR↑ | FPR↓ | Lat. (ms) | n_att | n_ben |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    tex = [
        "\\begin{table}[t]\\centering\\caption{Security and utility by configuration}\\label{tab:results}",
        "\\begin{tabular}{llrrrrrr}\\toprule",
        "Model & Config & ASR$\\downarrow$ & TMR$\\downarrow$ & DER$\\downarrow$ & TSR$\\uparrow$ & FPR$\\downarrow$ & Lat.\\ (ms) \\\\ \\midrule",
    ]
    for agent, config, s in rows:
        md.append(
            f"| {agent} | {config} | {pct(s.asr)}% | {pct(s.tmr)}% | {pct(s.der)}% | {pct(s.tsr)}% | {pct(s.fpr)}% | {s.latency_ms_mean:.0f} | {s.n_attack} | {s.n_benign} |"
        )
        tex.append(
            f"{agent} & {config} & {pct(s.asr)}\\% & {pct(s.tmr)}\\% & {pct(s.der)}\\% & {pct(s.tsr)}\\% & {pct(s.fpr)}\\% & {s.latency_ms_mean:.0f} \\\\"
        )
    tex += ["\\bottomrule\\end{tabular}\\end{table}"]
    return "\n".join(md), "\n".join(tex)


def table_vi(runs: dict[tuple[str, str], list[CaseResult]]) -> tuple[str, str]:
    md_lines = []
    tex_lines = [
        "\\begin{table}[t]\\centering\\caption{ASR by ablated configuration}\\label{tab:ablation}",
        "\\begin{tabular}{lrrr}\\toprule",
        "Ablation & ASR Email$\\downarrow$ & ASR RAG$\\downarrow$ & $p$ (McNemar vs.\\ C3) \\\\ \\midrule",
    ]
    agents = sorted({a for a, _ in runs})
    for agent in agents:
        base = runs.get((agent, "C3"))
        if base is None:
            continue
        md_lines += [
            f"**{agent}**",
            "",
            "| Ablation | ASR Email↓ | ASR RAG↓ | p (McNemar vs C3) |",
            "|---|---|---|---|",
        ]
        tex_lines.append(f"\\multicolumn{{4}}{{l}}{{\\textit{{{agent}}}}} \\\\")
        for config in ABLATIONS:
            results = runs.get((agent, config))
            if results is None:
                continue
            s = summarize(results)
            email = s.by_vector.get("email", Proportion(0, 0))
            rag = s.by_vector.get("rag", Proportion(0, 0))
            p = "-" if config == "C3" else f"{paired_comparison(results, base)['p_value']:.3g}"
            md_lines.append(f"| {LAYER_LABEL[config]} | {pct(email)}% | {pct(rag)}% | {p} |")
            tex_lines.append(f"{LAYER_LABEL[config]} & {pct(email)}\\% & {pct(rag)}\\% & {p} \\\\")
        md_lines.append("")
    tex_lines += ["\\bottomrule\\end{tabular}\\end{table}"]
    return "\n".join(md_lines), "\n".join(tex_lines)


def technique_table(runs: dict[tuple[str, str], list[CaseResult]]) -> str:
    configs = sorted({c for _, c in runs})
    agents = sorted({a for a, _ in runs})
    lines = []
    for agent in agents:
        techs: dict[str, dict[str, Proportion]] = defaultdict(dict)
        for config in configs:
            results = runs.get((agent, config))
            if not results:
                continue
            for t, p in summarize(results).by_technique.items():
                techs[t][config] = p
        lines += [
            f"### {agent}",
            "",
            "| technique | n | " + " | ".join(configs) + " |",
            "|---|---|" + "---|" * len(configs),
        ]
        for t in sorted(techs):
            n = next(iter(techs[t].values())).total
            lines.append(
                f"| {t} | {n} | "
                + " | ".join(pct(techs[t][c]) + "%" if c in techs[t] else "-" for c in configs)
                + " |"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("paper") / "tables")
    args = ap.parse_args(argv)
    runs: dict[tuple[str, str], list[CaseResult]] = {}
    for r in args.runs:
        runs.update(load_run(r))
    if not runs:
        print("no results found", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    md5, tex5 = table_v(runs)
    md6, tex6 = table_vi(runs)
    (args.out / "table_v_security_utility.md").write_text(md5 + "\n", encoding="utf-8")
    (args.out / "table_v_security_utility.tex").write_text(tex5 + "\n", encoding="utf-8")
    (args.out / "table_vi_ablation.md").write_text(md6 + "\n", encoding="utf-8")
    (args.out / "table_vi_ablation.tex").write_text(tex6 + "\n", encoding="utf-8")
    (args.out / "asr_by_technique.md").write_text(technique_table(runs) + "\n", encoding="utf-8")
    print(md5)
    print()
    print(md6)
    print(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
