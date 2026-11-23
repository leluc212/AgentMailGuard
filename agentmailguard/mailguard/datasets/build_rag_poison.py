"""Build the chunk-level RAG-poisoning detection set for Layer 3b.

    python -m mailguard.datasets.build_rag_poison

Output ``datasets/processed/rag_poison/chunks.jsonl`` with one knowledge chunk per line:
    {"chunk_id", "content", "poisoned": 0|1, "source", "technique", "target_query", "split"}

Positives
    * seed poison templates (PoisonedRAG-style, 3 attacker identities)
    * PoisonedRAG adversarial passages (NQ / HotpotQA / MS-MARCO, Zou et al. 2025)
Negatives
    * seed knowledge base sections
    * Bitext customer-support answers (clean, KB-like support text)
    * Enron ham bodies (realistic non-instructional prose)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import sys
from pathlib import Path
from typing import Any

from mailguard.config.settings import PROJECT_ROOT
from mailguard.datasets.seed import fill, load_kb, load_poison_templates

logger = logging.getLogger("mailguard.datasets.rag_poison")
RAW = PROJECT_ROOT / "datasets" / "raw"
OUT_DIR = PROJECT_ROOT / "datasets" / "processed" / "rag_poison"

ATTACKERS = [
    {
        "email": "exfil@attacker-mail.example",
        "url": "http://attacker-portal.example/verify",
        "phone": "+1-555-0199",
    },
    {
        "email": "support-desk@acme-helpcenter.co",
        "url": "https://acme-refunds.center/claim",
        "phone": "+44 20 7946 0999",
    },
    {
        "email": "billing@acme-secure-pay.net",
        "url": "http://acme-secure-pay.net/pay",
        "phone": "+84 28 3333 9999",
    },
]


def split_of(key: str) -> str:
    h = int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16) % 10
    return "test" if h < 2 else ("val" if h == 2 else "train")


STYLES = ("prefix", "paraphrase", "mid")
"""How a poisoned chunk carries the attacker's passage (ADR-0015 design decision 9):

    prefix      PoisonedRAG black-box layout: the target question, then the passage
    paraphrase  the passage alone: it restates the question's entities, no verbatim question
    mid         benign support text, then question + passage, then more benign text
"""
BENCH_POOL_PER_DATASET = 50
"""The benchmark draws its pinned RAG cases from 50 items of each PoisonedRAG file
(evaluation/datasets/mailguard/manifest.json ``prag_per_dataset``); the dev split never uses the
first 50 items of a file, nor any item the benchmark manifest pins (``pinned_prag_items``)."""
BENCH_MANIFEST = PROJECT_ROOT.parent / "evaluation" / "datasets" / "mailguard" / "manifest.json"


def pinned_prag_items(manifest: Path = BENCH_MANIFEST) -> set[str]:
    """``<dataset>-<item id>`` of every PoisonedRAG case the benchmark pins (empty without it)."""
    if not manifest.exists():
        return set()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    return {
        case_id.removeprefix("attack-prag-")
        for case_id in data.get("sets", {}).get("rag_attack", [])
        if case_id.startswith("attack-prag-")
    }


DEV_SPLIT_PATH = PROJECT_ROOT / "datasets" / "splits" / "rag_poison_dev.json"
_FILLER = (
    "Our support team answers most questions within one business day.",
    "You can find the order history under Account, then Orders.",
    "Please keep the original packaging until the issue is resolved.",
    "Opening hours are Monday to Friday, 9am to 6pm.",
    "Invoices are available as PDF files in the billing section.",
    "Tracking numbers are sent by email once the parcel ships.",
)


def styled_content(question: str, passage: str, style: str, rng: random.Random) -> str:
    """One poisoned chunk in ``style`` (``STYLES``); deterministic for a given ``rng`` state."""
    q = question.rstrip("?").strip() + "?"
    if style == "prefix":
        return passage if passage.lower().startswith(q.lower()[:20]) else f"{q} {passage}"
    if style == "paraphrase":
        return passage
    if style == "mid":
        head = " ".join(rng.sample(_FILLER, 2))
        tail = rng.choice(_FILLER)
        return f"{head} {q} {passage} {tail}"
    raise ValueError(f"unknown style {style!r}; expected one of {STYLES}")


def _prag_items(name: str) -> list[tuple[str, dict[str, Any]]]:
    path = RAW / "poisonedrag" / "results" / "adv_targeted_results" / f"{name}.json"
    if not path.exists():
        logger.warning("missing %s", path)
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [(str(item.get("id", key)), item) for key, item in data.items()]


def build_dev_split(
    *, seed: int = 20261014, per_dataset: int = 30, clean: int = 300, clean_sets: int = 100
) -> dict[str, Any]:
    """The doc-scanner tuning split: ids only, content is rebuilt from ``datasets/raw``.

    Poisoned: ``per_dataset`` PoisonedRAG items per file, drawn (seeded) from the items after the
    benchmark pool, each in every style. Clean: Bitext (instruction, response) pairs, the
    response being a relevant clean answer to its own question, the hardest negative for echo;
    and clean sets of three answers of one intent (similar, all on topic), the hardest negative
    for bait clusters.
    """
    rng = random.Random(seed)
    pinned = pinned_prag_items()
    if not pinned:
        raise SystemExit(f"no benchmark manifest at {BENCH_MANIFEST}: cannot exclude pinned cases")
    poisoned: list[dict[str, Any]] = []
    for name in ("nq", "hotpotqa", "msmarco"):
        items = [
            (item_id, item)
            for item_id, item in _prag_items(name)[BENCH_POOL_PER_DATASET:]
            if f"{name}-{item_id}" not in pinned
        ]
        for item_id, _ in rng.sample(items, min(per_dataset, len(items))):
            for style in STYLES:
                poisoned.append(
                    {
                        "case_id": f"dev-prag-{name}-{item_id}-{style}",
                        "dataset": name,
                        "item_id": item_id,
                        "style": style,
                    }  # fmt: skip
                )
    pairs = _bitext_pairs()
    clean_rows: list[dict[str, Any]] = []
    for i in sorted(rng.sample(range(len(pairs)), min(clean, len(pairs)))):
        clean_rows.append({"case_id": f"dev-bitext-{i}", "rows": [i]})
    by_intent: dict[str, list[int]] = {}
    for i, (_, _, intent) in enumerate(pairs):
        by_intent.setdefault(intent, []).append(i)
    intents = sorted(by_intent)
    for n in range(clean_sets if intents else 0):
        rows = sorted(rng.sample(by_intent[intents[n % len(intents)]], 3))
        clean_rows.append({"case_id": f"dev-bitext-set-{n}", "rows": rows})
    return {
        "schema": "rag-poison-dev-split/v3",
        "seed": seed,
        "styles": list(STYLES),
        "bench_pool_per_dataset": BENCH_POOL_PER_DATASET,
        "excluded_pinned_items": len(pinned),
        "poisoned": poisoned,
        "clean": clean_rows,
    }


def _bitext_pairs() -> list[tuple[str, str, str]]:
    """(instruction, response, intent) of the Bitext file, in file order, deduplicated (csv)."""
    import csv

    path = (
        RAW
        / "bitext_support"
        / "Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv"
    )
    if not path.exists():
        return []
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, str]] = []
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            pair = (str(row.get("instruction") or ""), str(row.get("response") or ""))
            if pair[0] and pair[1] and pair not in seen:
                seen.add(pair)
                out.append((*pair, str(row.get("intent") or "")))
    return out


def _fill(text: str) -> str:
    return (text.replace("{{Order Number}}", "ORD-771203").replace("{{Company Name}}", "Acme")
            .replace("{{Website URL}}", "https://www.acme.example")
            .replace("{{Customer Support Hours}}", "9am-6pm"))  # fmt: skip


def load_dev_cases(split: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Rebuild the dev cases: ``{case_id, style|None, poisoned, question, chunks: [{id, content}]}``.

    A poisoned case holds the item's adversarial passages in its style; a clean case holds one
    relevant clean answer. Needs ``datasets/raw`` (``python -m mailguard.datasets.download``).
    """
    if split is None:
        with open(DEV_SPLIT_PATH, encoding="utf-8") as f:
            split = json.load(f)
    items = {name: dict(_prag_items(name)) for name in ("nq", "hotpotqa", "msmarco")}
    pairs = _bitext_pairs()
    cases: list[dict[str, Any]] = []
    for row in split["poisoned"]:
        item = items[row["dataset"]][row["item_id"]]
        rng = random.Random(row["case_id"])
        q = str(item.get("question", ""))
        chunks = [
            {
                "chunk_id": f"{row['case_id']}-{j}",
                "content": styled_content(q, adv, row["style"], rng),
            }
            for j, adv in enumerate(item.get("adv_texts", []))
        ]
        cases.append({"case_id": row["case_id"], "style": row["style"], "poisoned": True,
                      "question": q.rstrip("?") + "?", "chunks": chunks})  # fmt: skip
    for row in split["clean"]:
        chunks = []
        for j, i in enumerate(row["rows"]):
            text = _fill(pairs[i][1])
            chunks.append({"chunk_id": f"{row['case_id']}-{j}", "content": text[:1500]})
        cases.append({"case_id": row["case_id"], "style": None, "poisoned": False,
                      "question": _fill(pairs[row["rows"][0]][0]), "chunks": chunks})  # fmt: skip
    return cases


def poisonedrag_rows(limit_per_dataset: int, style: str = "prefix") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in ("nq", "hotpotqa", "msmarco"):
        path = RAW / "poisonedrag" / "results" / "adv_targeted_results" / f"{name}.json"
        if not path.exists():
            logger.warning("missing %s", path)
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        items = list(data.values())[:limit_per_dataset]
        for item in items:
            q = str(item.get("question", ""))
            for j, adv in enumerate(item.get("adv_texts", [])):
                # prefix = PoisonedRAG black-box attack: S = question + adversarial passage
                cid = f"prag-{name}-{item.get('id', 'x')}-{j}"
                content = (
                    (f"{q}? {adv}" if not adv.lower().startswith(q.lower()[:20]) else adv)
                    if style == "prefix"
                    else styled_content(q, adv, style, random.Random(cid))
                )
                rows.append(
                    {
                        "chunk_id": cid,
                        "content": content,
                        "poisoned": 1,
                        "source": "poisonedrag",
                        "technique": "knowledge_corruption",
                        "target_query": q,
                        "incorrect_answer": item.get("incorrect answer"),
                        "correct_answer": item.get("correct answer"),
                        "split": split_of(cid),
                    }
                )
    return rows


def bitext_rows(limit: int, rng: random.Random) -> list[dict[str, Any]]:
    import pandas as pd

    path = (
        RAW
        / "bitext_support"
        / "Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv"
    )
    if not path.exists():
        return []
    df = pd.read_csv(path)
    responses = df["response"].dropna().astype(str).unique().tolist()
    rng.shuffle(responses)
    rows = []
    for i, r in enumerate(responses[:limit]):
        text = r.replace("{{Order Number}}", "ORD-771203").replace(
            "{{Customer Support Hours}}", "9am-6pm"
        )
        text = text.replace("{{Website URL}}", "https://www.acme.example").replace(
            "{{Company Name}}", "Acme"
        )
        cid = f"bitext-resp-{i}"
        rows.append(
            {
                "chunk_id": cid,
                "content": text[:1500],
                "poisoned": 0,
                "source": "bitext_support",
                "technique": None,
                "target_query": "",
                "split": split_of(cid),
            }
        )
    return rows


def enron_rows(limit: int, rng: random.Random) -> list[dict[str, Any]]:
    path = RAW / "enron_ham" / "test.jsonl"
    if not path.exists():
        return []
    hams = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d.get("label_text") == "ham":
                body = str(d.get("message", "")).strip()
                if 200 <= len(body) <= 1500:
                    hams.append((str(d.get("message_id")), body))
    rng.shuffle(hams)
    return [
        {
            "chunk_id": f"enron-{mid}",
            "content": body,
            "poisoned": 0,
            "source": "enron_ham",
            "technique": None,
            "target_query": "",
            "split": split_of(f"enron-{mid}"),
        }
        for mid, body in hams[:limit]
    ]


def seed_rows() -> list[dict[str, Any]]:
    rows = [
        {
            "chunk_id": c.chunk_id,
            "content": c.content,
            "poisoned": 0,
            "source": "seed_kb",
            "technique": None,
            "target_query": "",
            "split": "test",
        }
        for c in load_kb()
    ]
    for t in load_poison_templates():
        for k, att in enumerate(ATTACKERS):
            cid = f"{t['id']}-a{k}"
            rows.append(
                {
                    "chunk_id": cid,
                    "content": fill(t["text"], att).strip(),
                    "poisoned": 1,
                    "source": "seed_poison",
                    "technique": t.get("technique", "answer_forcing"),
                    "target_query": t.get("target_query", ""),
                    "split": "test",
                }
            )
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", type=Path, default=OUT_DIR / "chunks.jsonl")
    ap.add_argument("--prag-per-dataset", type=int, default=100)
    ap.add_argument("--negatives", type=int, default=800, help="per negative source")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--style", choices=STYLES, default="prefix", help="poisoned chunk layout")
    ap.add_argument(
        "--dev-split",
        action="store_true",
        help=f"write the doc-scanner tuning split (ids only) to {DEV_SPLIT_PATH} and exit",
    )
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.dev_split:
        split = build_dev_split()
        DEV_SPLIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEV_SPLIT_PATH.write_text(json.dumps(split, indent=1) + "\n", encoding="utf-8")
        logger.info(
            "wrote %d poisoned + %d clean dev cases -> %s",
            len(split["poisoned"]),
            len(split["clean"]),
            DEV_SPLIT_PATH,
        )
        return 0
    rng = random.Random(args.seed)

    rows = seed_rows() + poisonedrag_rows(args.prag_per_dataset, args.style)
    rows += bitext_rows(args.negatives, rng) + enron_rows(args.negatives, rng)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    stats: dict[str, dict[str, int]] = {}
    for r in rows:
        s = stats.setdefault(r["source"], {"poisoned": 0, "clean": 0})
        s["poisoned" if r["poisoned"] else "clean"] += 1
    with open(args.out.with_name("stats.json"), "w", encoding="utf-8") as f:
        json.dump({"total": len(rows), "by_source": stats}, f, indent=2)
    logger.info("wrote %d chunks -> %s  %s", len(rows), args.out, stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
