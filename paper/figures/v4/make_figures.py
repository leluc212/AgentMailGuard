"""Figures and tables of the v4 benchmark (task 7.36; R22.12). Reads only committed run outputs.

    uv run --project . --with matplotlib python paper/figures/v4/make_figures.py

Inputs, per model run ``evaluation/results/mailguard_bench/<run>/``:
  cases.jsonl              each case's kind, source and scenario (the attack groups)
  ragemail__<config>.jsonl the report's per-case score (``goal_achieved``); error rows are absent
  metrics.csv              the report's own totals; every group total here must equal them
  case_manifest.json       which ids are the email, RAG and synthetic sets

Nothing is typed in by hand: no number, no count. A model is drawn only if its run folder has
both configs scored. Outputs go to ``paper/figures/v4/out/`` (PDF and PNG, CSV and LaTeX tables).

Attack success rate (ASR) = attacks whose goal is in the final draft / attacks scored, over every
scored attack (a case triage stopped is a failed attack). 95 % Wilson intervals. McNemar exact
p from the report (C0 vs C3, paired on the same cases).
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "evaluation" / "results" / "mailguard_bench"
OUT = Path(__file__).resolve().parent / "out"
MODELS = (  # (label in the figures, run folder)
    ("gpt-4o-mini", "2026-11-10-gpt4omini-v4"),
    ("Llama-3.1-8B", "2026-11-11-llama31-v4"),
    # ADR-0021 (owner, 2026-11-15): Qwen3.5-9B replaces Qwen2.5-7B (`2026-11-11-qwen25-local-v4`,
    # kept in the repository, not reported).
    ("Qwen3.5-9B", "2026-11-16-qwen35-9b-v4"),
)
CONFIGS = ("C0", "C3")
CONFIG_LABEL = {"C0": "C0 (no guard)", "C3": "C3 (full guard)"}

# The attack groups: (set in case_manifest, figure, group label, predicate on a case)
GROUPS = (
    ("llmail_attack_ids", "email", "LLMail-Inject phase 2, scenario 1 (level1*)",
     lambda c: str(c.get("meta", {}).get("scenario", "")).startswith("level1")),
    ("llmail_attack_ids", "email", "LLMail-Inject phase 2, scenario 2 (level2*)",
     lambda c: str(c.get("meta", {}).get("scenario", "")).startswith("level2")),
    ("rag_attack_ids", "rag", "PoisonedRAG (wrong answer)", lambda c: c["source"] == "poisonedrag"),
    ("rag_attack_ids", "rag", "AgentMailGuard seed RAG (wrong answer)", lambda c: c["source"] == "seed_rag"),
    ("rag_attack_ids", "rag", "LLMail-Inject phase 1, scenario 3 (exfiltration)",
     lambda c: c["source"] == "llmail_inject_phase1" and str(c.get("meta", {}).get("scenario")) == "3"),
    ("rag_attack_ids", "rag", "LLMail-Inject phase 1, scenario 4 (exfiltration)",
     lambda c: c["source"] == "llmail_inject_phase1" and str(c.get("meta", {}).get("scenario")) == "4"),
    ("rag_synthetic_ids", "synthetic", "synthetic_poison_v1 (reported apart)",
     lambda c: c["source"] == "synthetic_poison_v1"),
)  # fmt: skip
TOTALS = {"email": ("llmail", "Email attacks, all"), "rag": ("rag", "RAG attacks, all"),
          "synthetic": ("rag_synthetic", "Synthetic RAG attacks, all")}  # fmt: skip
PAIRED = {"email": "LLMail-Inject C0 vs C3", "rag": "RAG vector C0 vs C3",
          "synthetic": "RAG synthetic C0 vs C3"}  # fmt: skip
FIGURES = (
    ("email", "fig1_asr_email", "Attack success rate, email attacks (LLMail-Inject phase 2)"),
    ("rag", "fig2_asr_rag", "Attack success rate, RAG attacks (poisoned knowledge documents)"),
    ("synthetic", "fig3_asr_synthetic", "Attack success rate, synthetic RAG attacks (reported apart)"),
)


def wilson(k: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").split("\n") if x.strip()]


def load_run(run: str) -> dict | None:
    d = RESULTS / run
    if not all((d / f"ragemail__{c}.jsonl").is_file() for c in CONFIGS):
        return None
    cases = {c["case_id"]: c for c in read_jsonl(d / "cases.jsonl")}
    manifest = json.loads((d / "case_manifest.json").read_text(encoding="utf-8"))
    scored = {c: {r["case_id"]: bool(r["goal_achieved"]) for r in read_jsonl(d / f"ragemail__{c}.jsonl")
                  if r["kind"] == "attack"} for c in CONFIGS}  # fmt: skip
    metrics = list(csv.DictReader((d / "metrics.csv").open(encoding="utf-8")))
    return {"cases": cases, "manifest": manifest, "scored": scored, "metrics": metrics}


def metric(rows: list[dict], table: str, config: str, name: str) -> tuple[int, int]:
    for r in rows:
        if r["table"] == table and r["config"] == config and r["metric"] == name and r["group"] == "all":
            return int(r["successes"]), int(r["total"])
    raise KeyError((table, config, name))


def paired_p(rows: list[dict], name: str) -> float | None:
    for r in rows:
        if r["table"] == "paired" and r["config"] == name and r["metric"] == "mcnemar_exact_p":
            return float(r["value"])
    return None


def tally(run: dict) -> dict:
    """Per (figure, group label, config): k successes, n scored, planned; checked against metrics."""
    out: dict = {}
    for set_key, fig, label, pred in GROUPS:
        ids = [i for i in run["manifest"].get(set_key) or [] if pred(run["cases"][i])]
        for c in CONFIGS:
            got = [run["scored"][c][i] for i in ids if i in run["scored"][c]]
            out[(fig, label, c)] = (sum(got), len(got), len(ids))
    for fig, (table, label) in TOTALS.items():
        for c in CONFIGS:
            k = sum(v[0] for (f, _, cc), v in out.items() if f == fig and cc == c)
            n = sum(v[1] for (f, _, cc), v in out.items() if f == fig and cc == c)
            planned = sum(v[2] for (f, _, cc), v in out.items() if f == fig and cc == c)
            mk, mn = metric(run["metrics"], table, c, "ASR")
            if (k, n) != (mk, mn):
                raise SystemExit(f"group sum {k}/{n} != metrics.csv {table} {c} ASR {mk}/{mn}")
            out[(fig, label, c)] = (k, n, planned)
    return out


def fmt_pct(k: int, n: int) -> str:
    return f"{100 * k / n:.1f} %" if n else "n/a"


def check_inside(fig, name: str) -> None:
    """Fail if any text of the figure is drawn outside it (a label cut off in the PDF)."""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    box = fig.bbox
    for text in fig.findobj(matplotlib.text.Text):
        if not text.get_visible() or not text.get_text().strip():
            continue
        e = text.get_window_extent(renderer)
        if e.x0 < box.x0 - 1 or e.x1 > box.x1 + 1 or e.y0 < box.y0 - 1 or e.y1 > box.y1 + 1:
            raise SystemExit(f"{name}: text {text.get_text()!r} is outside the figure")


def _overlap(a, b, tol: float = 0.5) -> bool:
    return min(a.x1, b.x1) - max(a.x0, b.x0) > tol and min(a.y1, b.y1) - max(a.y0, b.y0) > tol


def check_no_overlap(fig, name: str, value_texts: list, bars: list, note) -> None:
    """Fail if a value label sits on a bar or on another label, or the note runs into an axis."""
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    boxes = [t.get_window_extent(r) for t in value_texts]
    # A bar must be at least as tall as its value label (points; 72 per inch), or the panel was
    # squeezed and the C0 and C3 bars and labels run into each other.
    min_px = 0.8 * max(b.height for b in boxes)
    for bar in bars:
        if bar.get_window_extent(r).height < min_px:
            raise SystemExit(f"{name}: bars are {bar.get_window_extent(r).height:.1f} px tall, "
                             f"labels {min_px / 0.8:.1f} px: the panel is too short")  # fmt: skip
    for i, b in enumerate(boxes):
        for bar in bars:
            if bar.get_width() > 0 and _overlap(b, bar.get_window_extent(r)):
                raise SystemExit(f"{name}: value label {value_texts[i].get_text()!r} overlaps a bar")
        for j in range(i + 1, len(boxes)):
            if _overlap(b, boxes[j]):
                raise SystemExit(f"{name}: value labels {value_texts[i].get_text()!r} and "
                                 f"{value_texts[j].get_text()!r} overlap")  # fmt: skip
    nb = note.get_window_extent(r)
    for ax in fig.axes:
        if _overlap(nb, ax.get_tightbbox(r)):
            raise SystemExit(f"{name}: the note overlaps a panel")


def draw(fig_key: str, name: str, title: str, models: list[tuple[str, dict]]) -> None:
    labels = [g[2] for g in GROUPS if g[1] == fig_key] + [TOTALS[fig_key][1]]
    if fig_key == "synthetic":
        labels = [TOTALS[fig_key][1]]
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9})
    # Sizes in inches, so a panel with one group is as readable as one with six: each group row
    # 0.55, each panel's title, ticks and axis label 0.85, the figure title 0.35, the note 0.6.
    panel_in = 0.55 * len(labels) + 0.85
    title_in, note_in = 0.35, 0.6
    height = title_in + panel_in * len(models) + note_in
    fig, axes = plt.subplots(len(models), 1, figsize=(7.2, height), squeeze=False)
    value_texts, bars = [], []
    for ax, (model, t) in zip(axes[:, 0], models, strict=True):
        y = list(range(len(labels)))[::-1]
        h = 0.36
        for j, c in enumerate(CONFIGS):
            vals, los, his, texts = [], [], [], []
            for label in labels:
                k, n, planned = t[(fig_key, label, c)]
                p = k / n if n else 0.0
                lo, hi = wilson(k, n)
                vals.append(100 * p)
                # max(0, .): at k = 0 the Wilson bound is p up to float rounding (-1e-17)
                los.append(max(0.0, 100 * (p - lo)))
                his.append(max(0.0, 100 * (hi - p)))
                texts.append(fmt_pct(k, n))  # owner 2026-11-11: percent only; k/n is in the table
            ys = [v + (h / 2 if j == 0 else -h / 2) for v in y]
            style = dict(color="white", edgecolor="black", hatch="///") if c == "C0" else dict(color="black", edgecolor="black")
            cont = ax.barh(ys, vals, height=h, xerr=[los, his], capsize=2,
                           error_kw={"elinewidth": 0.8, "ecolor": "0.35"}, label=CONFIG_LABEL[c], **style)  # fmt: skip
            bars.extend(cont.patches)
            for yy, v, hi_err, txt in zip(ys, vals, his, texts, strict=True):
                value_texts.append(ax.text(v + hi_err + 1.0, yy, txt, va="center", fontsize=7.5))
        ax.set_ylim(-0.6, len(labels) - 0.4)
        ax.set_yticks(y)
        ax.set_yticklabels(labels, fontsize=8)
        # Room right of 100 % for the value labels; the scale itself stops at 100 %.
        ax.set_xlim(0, 128)
        ax.set_xticks(range(0, 101, 20))
        ax.spines["bottom"].set_bounds(0, 100)
        ax.set_xlabel("Attack success rate (%)")
        ax.set_title(model, fontsize=9, loc="left")
        ax.grid(axis="x", color="0.85", linewidth=0.6)
        ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
    axes[0, 0].legend(loc="lower right", fontsize=8, frameon=False)
    fig.suptitle(title, fontsize=10, x=0.01, ha="left")
    note = ("Attack success rate = attacks whose goal is in the final draft / attacks scored; a case "
            "stopped by triage counts as a failed attack.\nError bars: 95 % Wilson interval. Cases that "
            "ended in an error after the retry pass are not scored. C0 and C3 run on the same cases.\n"
            "Counts (k/n) and McNemar p are in Table table_asr.")  # fmt: skip
    note_text = fig.text(0.01, 0.005, note, fontsize=6.8, va="bottom", ha="left")
    fig.tight_layout(rect=(0, note_in / height, 1, 1 - title_in / height))
    check_inside(fig, name)
    check_no_overlap(fig, name, value_texts, bars, note_text)
    for ext in ("pdf", "png"):
        fig.savefig(OUT / f"{name}.{ext}", dpi=300 if ext == "png" else None)
    plt.close(fig)


def write_tables(models: list[tuple[str, dict, dict]]) -> None:
    head = ["Model", "Attack group", "Cases planned", "Config", "k", "n", "Attack success rate (%)",
            "Wilson 95 % low (%)", "Wilson 95 % high (%)", "McNemar exact p (C0 vs C3)"]  # fmt: skip
    rows = []
    for model, t, run in models:
        for fig_key, _, _ in FIGURES:
            labels = [g[2] for g in GROUPS if g[1] == fig_key] + [TOTALS[fig_key][1]]
            if fig_key == "synthetic":
                labels = [TOTALS[fig_key][1]]
            for label in labels:
                for c in CONFIGS:
                    k, n, planned = t[(fig_key, label, c)]
                    lo, hi = wilson(k, n)
                    p = paired_p(run["metrics"], PAIRED[fig_key]) if label == TOTALS[fig_key][1] else None
                    rows.append([model, label, planned, CONFIG_LABEL[c], k, n,
                                 f"{100 * k / n:.1f}" if n else "", f"{100 * lo:.1f}" if n else "",
                                 f"{100 * hi:.1f}" if n else "",
                                 ("" if p is None else (f"{p:.1e}" if c == "C3" else ""))])  # fmt: skip
    with (OUT / "table_asr.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(head)
        w.writerows(rows)
    tex = ["\\begin{tabular}{llrlrrrl}", "\\hline",
           "Model & Attack group & Planned & Config & $k/n$ & ASR (\\%) & 95\\% CI & McNemar $p$ \\\\", "\\hline"]  # fmt: skip
    for r in rows:
        ci = f"[{r[7]}, {r[8]}]" if r[6] else ""
        tex.append(f"{r[0]} & {r[1]} & {r[2]} & {r[3]} & {r[4]}/{r[5]} & {r[6]} & {ci} & {r[9]} \\\\")
    tex += ["\\hline", "\\end{tabular}"]
    (OUT / "table_asr.tex").write_text("\n".join(tex).replace("%", "\\%").replace("\\\\%", "\\%") + "\n",
                                       encoding="utf-8", newline="\n")  # fmt: skip


def write_benign_table(models: list[tuple[str, dict, dict]]) -> None:
    """FPR and utility on the 500 benign emails (in the table only, owner 2026-11-11)."""
    head = ["Model", "Config", "Metric", "k", "n", "Rate (%)", "Wilson 95 % low (%)", "Wilson 95 % high (%)"]
    # Definitions are the report's (artifacts.py): FPR counts blocked or quarantined benign emails
    # over all scored benign; guard FPR counts any guard escalation (human approval included)
    # over the benign emails that reached drafting; utility is a draft that was not blocked and
    # has at least 40 characters (ADR-0012 decision 2(e)).
    names = (("FPR", "False-positive rate (blocked or quarantined), all benign emails"),
             ("guard_FPR", "False-positive rate (any guard escalation), benign emails that reached drafting"),
             ("benign_utility", "Benign utility (draft not blocked, at least 40 characters)"))  # fmt: skip
    rows = []
    for model, _, run in models:
        for c in CONFIGS:
            for key, label in names:
                k, n = metric(run["metrics"], "llmail", c, key)
                lo, hi = wilson(k, n)
                rows.append([model, CONFIG_LABEL[c], label, k, n, f"{100 * k / n:.1f}" if n else "",
                             f"{100 * lo:.1f}" if n else "", f"{100 * hi:.1f}" if n else ""])  # fmt: skip
    with (OUT / "table_benign.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(head)
        w.writerows(rows)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    loaded = []
    for model, run in MODELS:
        r = load_run(run)
        if r is None:
            print(f"skip {model}: {run} has no scored C0 and C3")
            continue
        loaded.append((model, tally(r), r))
        print(f"ok {model}: group totals equal metrics.csv")
    for fig_key, name, title in FIGURES:
        draw(fig_key, name, title, [(m, t) for m, t, _ in loaded])
    write_tables(loaded)
    write_benign_table(loaded)
    print(f"ok figures and tables -> {OUT}")


if __name__ == "__main__":
    main()
