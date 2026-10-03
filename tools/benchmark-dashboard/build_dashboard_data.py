"""Refresh the embedded data block of tools/benchmark-dashboard/index.html. Read-only on results.

    .venv\\Scripts\\python.exe tools\\benchmark-dashboard\\build_dashboard_data.py

For each model run below it reads evaluation/results/mailguard_bench/<run>/{metrics.csv,
manifest.json, kit-log.jsonl} and, for the pre-L5 TMR, tools/analysis/tmr_pre_l5.py's output
(regenerated here from raw/*.jsonl). A run with no metrics.csv yet is embedded as "running" with a
progress snapshot. Only the text between the BEGIN/END GENERATED DATA markers in index.html is
rewritten. Nothing in the benchmark or the guard is imported or modified.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RESULTS = REPO / "evaluation" / "results" / "mailguard_bench"
HTML = HERE / "index.html"
sys.path.insert(0, str(REPO / "tools" / "analysis"))
import tmr_pre_l5  # noqa: E402  (our own read-only analysis script)

CONFIGS = list(tmr_pre_l5.CONFIGS)
CASES_PER_CONFIG = 550
MODELS = [
    {"key": "gpt", "name": "GPT-4o mini", "run": "2026-10-01-gpt4omini-live",
     "provider": "OpenAI (direct)"},
    {"key": "qwen", "name": "Qwen2.5-7B Instruct", "run": "2026-10-02-qwen25-openrouter-live",
     "provider": "OpenRouter · pinned to Phala, no fallbacks"},
    {"key": "llama", "name": "Llama-3.1-8B Instruct", "run": "2026-10-02-llama31-openrouter-live",
     "provider": "OpenRouter · pinned to CoreWeave (bf16), no fallbacks",
     # Checked in raw/C2.jsonl and raw/C7.jsonl: 85 of 100 RAG cases end inbound on
     # P01-critical-injection-quarantine because Llama's L2 extractor reports the user's plain
     # factual question as "instructions aimed at the assistant". Qwen and GPT-4o mini do not.
     "caveat": "Llama's low RAG ASR in <b>C2 (7%)</b> and <b>C7 (0%)</b> is not poisoning detection. "
               "In 85 of 100 RAG cases its own L2 extractor flagged the customer's plain factual "
               "question as instructions to the assistant, and L5 quarantined the email before "
               "retrieval mattered. That is over-blocking of a benign-looking question; L3b alone "
               "(C4) is the fair RAG figure (54%)."},
]
BEGIN, END = "/* BEGIN GENERATED DATA */", "/* END GENERATED DATA */"
SUP = str.maketrans("-0123456789", "⁻⁰¹²³⁴⁵⁶⁷⁸⁹")


def p_text(p: float) -> str:
    if p >= 0.001:
        return f"{p:.3f}"
    mant, exp = f"{p:.2e}".split("e")
    return f"{mant}×10{str(int(exp)).translate(SUP)}"


def duration(run_dir: Path) -> str:
    log = run_dir / "kit-log.jsonl"
    if not log.exists():
        return ""
    secs = sum(float(json.loads(l).get("seconds") or 0) for l in open(log, encoding="utf-8")
               if l.strip() and json.loads(l).get("step") == "config")
    return f"{int(secs // 3600)}h {int(secs % 3600 // 60):02d}m"


def run_date(run_dir: Path) -> str:
    path = run_dir / "manifest.json"
    if path.exists():
        ts = json.load(open(path, encoding="utf-8")).get("timestamp")
        if ts:
            return datetime.fromisoformat(ts).strftime("%d %b %Y")
    return ""


def metrics(run_dir: Path) -> dict:
    m: dict = {}
    with open(run_dir / "metrics.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            m[(r["table"], r["config"], r["metric"], r["group"])] = r
    return m


def pct(m, table, cfg, metric, group="all") -> float:
    r = m.get((table, cfg, metric, group))
    return float(r["pct"]) if r and r["pct"] != "" else 0.0


def val(m, table, cfg, metric) -> float:
    r = m.get((table, cfg, metric, ""))
    return float(r["value"]) if r and r["value"] not in ("", None) else 0.0


def complete(model: dict, run_dir: Path) -> dict:
    m = metrics(run_dir)
    if tmr_pre_l5.analyse(model["run"]) != 0:
        raise SystemExit(f"tmr_pre_l5 cross-check failed for {model['run']}")
    tmr = {(r["config"], r["vector"]): r for r in csv.DictReader(open(run_dir / "tmr_pre_l5.csv", encoding="utf-8"))}
    errors = int(sum(val(m, t, c, "errors") for t in ("llmail", "rag") for c in CONFIGS))
    triage = int(sum(val(m, t, c, "triage_stage_failure_errors") for t in ("llmail", "rag") for c in CONFIGS))
    email_n = [int(m[("llmail", c, "ASR", "all")]["total"]) for c in CONFIGS]
    fpr7 = m[("llmail", "C7", "FPR", "all")]
    reached7 = m.get(("llmail", "C7", "guard_FPR", "all"))
    pe = m.get(("paired", "LLMail-Inject C0 vs C7", "mcnemar_exact_p", ""))
    pr = m.get(("paired", "RAG vector C0 vs C7", "mcnemar_exact_p", ""))
    return {
        **model, "status": "complete", "date": run_date(run_dir), "duration": duration(run_dir),
        "executions": CASES_PER_CONFIG * len(CONFIGS), "errors": errors,
        "ok": CASES_PER_CONFIG * len(CONFIGS) - errors, "triageErrors": triage,
        "emailN": email_n,
        "emailASR": [pct(m, "llmail", c, "ASR") for c in CONFIGS],
        "ragASR": [pct(m, "rag", c, "ASR") for c in CONFIGS],
        "tmrEmail": [float(tmr[(c, "email")]["TMR_pre_pct"]) for c in CONFIGS],
        "tmrRag": [float(tmr[(c, "rag")]["TMR_pre_pct"]) for c in CONFIGS],
        "preEmail": [float(tmr[(c, "email")]["GSR_pre_pct"]) for c in CONFIGS],
        "preRag": [float(tmr[(c, "rag")]["GSR_pre_pct"]) for c in CONFIGS],
        "removedAll": [int(tmr[(c, "all")]["caught_after_generation"]) for c in CONFIGS],
        "derC0": pct(m, "all", "C0", "DER"), "derC7": pct(m, "all", "C7", "DER"),
        "fprC0": pct(m, "llmail", "C0", "FPR"), "fprC7": float(fpr7["pct"]),
        "fprC7n": f"{fpr7['successes']}/{fpr7['total']}",
        "reachedC7": int(reached7["total"]) if reached7 else None,
        "utilC0": pct(m, "llmail", "C0", "benign_utility"), "utilC7": pct(m, "llmail", "C7", "benign_utility"),
        "pEmail": p_text(float(pe["value"])) if pe else "n/a",
        "pRag": p_text(float(pr["value"])) if pr else "n/a",
        "latP50": f"{val(m, 'overhead', 'C7', 'latency_total_ms_p50') / 1000:.2f} s",
        "latP95": f"{val(m, 'overhead', 'C7', 'latency_total_ms_p95') / 1000:.2f} s",
        "cost": f"${val(m, 'overhead', 'C7', 'cost_per_email_usd'):.6f}",
        "guardCallsC7": round(val(m, "overhead", "C7", "guard_calls_per_email"), 2),
    }


def running(model: dict, run_dir: Path) -> dict:
    done, rows = [], 0
    log = run_dir / "kit-log.jsonl"
    if log.exists():
        for line in open(log, encoding="utf-8"):
            e = json.loads(line) if line.strip() else {}
            if e.get("step") == "config" and e.get("status") == "ok" and e.get("config") not in done:
                done.append(e["config"])
    for c in CONFIGS:
        raw = run_dir / "raw" / f"{c}.jsonl"
        if raw.exists():
            rows += len({json.loads(l)["case_id"] for l in open(raw, encoding="utf-8") if l.strip()})
    return {**model, "status": "running", "configsDone": done, "rows": rows,
            "total": CASES_PER_CONFIG * len(CONFIGS),
            "snapshot": datetime.now(timezone.utc).astimezone().strftime("%d %b %Y %H:%M")}


def main() -> int:
    data = []
    for model in MODELS:
        run_dir = RESULTS / model["run"]
        data.append(complete(model, run_dir) if (run_dir / "metrics.csv").exists() else running(model, run_dir))
    block = f"{BEGIN}\nconst DATA={json.dumps(data, ensure_ascii=False, indent=1)};\n{END}"
    html = HTML.read_text(encoding="utf-8")
    a, b = html.index(BEGIN), html.index(END) + len(END)
    HTML.write_text(html[:a] + block + html[b:], encoding="utf-8")
    for d in data:
        print(f"ok {d['key']}: {d['status']}" + (f" ({d['rows']}/{d['total']} rows)" if d["status"] == "running" else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
