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


def poisonedrag_rows(limit_per_dataset: int) -> list[dict]:
    rows: list[dict] = []
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
                # PoisonedRAG black-box attack: S = question + adversarial passage
                content = f"{q}? {adv}" if not adv.lower().startswith(q.lower()[:20]) else adv
                cid = f"prag-{name}-{item.get('id', 'x')}-{j}"
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


def bitext_rows(limit: int, rng: random.Random) -> list[dict]:
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


def enron_rows(limit: int, rng: random.Random) -> list[dict]:
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


def seed_rows() -> list[dict]:
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
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rng = random.Random(args.seed)

    rows = seed_rows() + poisonedrag_rows(args.prag_per_dataset)
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
