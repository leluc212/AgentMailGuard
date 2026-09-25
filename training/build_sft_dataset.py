"""Turn the L1 corpus into chat-format SFT data for the LLM judge (Qwen / Llama LoRA).

    python -m training.build_sft_dataset            # -> datasets/processed/sft_judge/{train,val}.jsonl

Each line: {"messages": [{"role": "system", ...}, {"role": "user", ...}, {"role": "assistant", ...}]}
where the assistant turn is the JSON object the judge must emit (see prompts/l1_judge.v1.txt).
Labels come from the corpus; confidence is set to 0.95 / 0.05 and the technique tag is the
corpus technique when known. Emails are wrapped in the same nonce markers used at inference.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path

from mailguard.config.settings import PROJECT_ROOT
from mailguard.contracts.email import GuardedEmail
from mailguard.layers.l1_injection_scanner.llm_judge import build_judge_messages

logger = logging.getLogger("training.sft")
CORPUS = PROJECT_ROOT / "datasets" / "processed" / "l1_injection"
OUT = PROJECT_ROOT / "datasets" / "processed" / "sft_judge"

RATIONALE_POS = (
    "The text contains instructions aimed at the AI assistant rather than a human reader."
)
RATIONALE_NEG = (
    "The text is an ordinary request or message with no instructions aimed at the assistant."
)


def to_example(row: dict) -> dict:
    text = str(row["text"])
    subject, _, body = text.partition("\n") if text.startswith("Subject:") else ("", "", text)
    email = GuardedEmail(subject=subject.replace("Subject:", "").strip(), body_text=body.strip())
    messages = build_judge_messages(email, max_chars=6000)
    label = int(row["label"]) == 1
    target = {
        "is_injection": label,
        "confidence": 0.95 if label else 0.05,
        "techniques": [row["technique"]] if (label and row.get("technique")) else [],
        "injected_instructions": [],
        "rationale": RATIONALE_POS if label else RATIONALE_NEG,
    }
    return {
        "id": row["id"],
        "source": row["source"],
        "messages": [{"role": m.role, "content": m.content} for m in messages]
        + [{"role": "assistant", "content": json.dumps(target, ensure_ascii=False)}],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--corpus", type=Path, default=CORPUS)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--max-per-split", type=int, default=12000)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rng = random.Random(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    for split in ("train", "val"):
        src = args.corpus / f"{split}.jsonl"
        if not src.exists():
            logger.warning("missing %s", src)
            continue
        rows = [json.loads(line) for line in open(src, encoding="utf-8") if line.strip()]
        rng.shuffle(rows)
        rows = rows[: args.max_per_split]
        with open(args.out / f"{split}.jsonl", "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(to_example(r), ensure_ascii=False) + "\n")
        pos = sum(int(r["label"]) for r in rows)
        logger.info(
            "%s: %d examples (%d positive) -> %s",
            split,
            len(rows),
            pos,
            args.out / f"{split}.jsonl",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
