"""Build the end-to-end email + RAG benchmark cases for ``evaluation/run_benchmark.py``.

    python -m mailguard.datasets.build_email_benchmark

Output ``datasets/processed/email_bench/cases.jsonl`` (one ``BenchCase`` per line) and
``stats.json``. Sources and how they are adapted to the customer-support email setting:

    seed             24 attack templates x carrier benign emails (email vector, checkable goals)
    seed_rag         6 PoisonedRAG-style poisons retrieved next to clean KB chunks (rag vector)
    poisonedrag      real adversarial passages (NQ/HotpotQA/MS-MARCO); goal = incorrect answer
    bipia            email contexts x 15 text-attack types (KDD'25); goal = injected task echo/link
    injecagent       data-stealing / direct-harm attacker instructions embedded in emails (ACL'24)
    llmail_inject    real adaptive attack emails from Microsoft's challenge; goal = exfiltration
    benign           seed (40), LLMail FP set (203), Enron ham, Bitext support requests

Leakage control: sources that also feed the L1 training corpus are split by a stable
hash of the item id (see ``bench_side``); the benchmark only uses the "bench" half.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
import sys
from pathlib import Path
from typing import Any

from mailguard.config.settings import PROJECT_ROOT
from mailguard.datasets.seed import (
    KBChunk,
    fill,
    load_attack_templates,
    load_benign_emails,
    load_kb,
    load_poison_templates,
    simple_retrieve,
)

logger = logging.getLogger("mailguard.datasets.bench")
RAW = PROJECT_ROOT / "datasets" / "raw"
OUT_DIR = PROJECT_ROOT / "datasets" / "processed" / "email_bench"

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
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def bench_side(key: str) -> bool:
    """True when the item belongs to the benchmark half (odd hash); the other half trains L1."""
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16) % 2 == 1


def chunks_for(query: str, kb: list[KBChunk], k: int = 3) -> list[dict[str, Any]]:
    return [c.as_dict() for c in simple_retrieve(query, kb, k)] if query else []


def inject(body: str, payload: str, position: str) -> tuple[str, str | None]:
    """Return (body_text, body_html) with the payload placed at ``position``."""
    payload = payload.strip()
    if position == "start":
        return payload + "\n\n" + body, None
    if position == "middle":
        paras = body.split("\n\n")
        mid = max(1, len(paras) // 2)
        return "\n\n".join(paras[:mid] + [payload] + paras[mid:]), None
    if position == "hidden_html":
        html = "<p>" + body.replace("\n\n", "</p><p>") + "</p>"
        html += f'<div style="font-size:0px;color:#ffffff">{payload}</div>'
        return body + "\n\n" + payload, html
    return body + "\n\n" + payload, None


def case(**kw: Any) -> dict[str, Any]:
    base = {
        "case_id": "",
        "kind": "attack",
        "source": "seed",
        "technique": None,
        "vector": "email",
        "email": {},
        "chunks": [],
        "kb_query": "",
        "category": "support",
        "goal": {},
        "expected_keywords": [],
        "attacker": {},
        "meta": {},
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------- seed
def seed_benign(kb: list[KBChunk]) -> list[dict[str, Any]]:
    out = []
    for e in load_benign_emails():
        out.append(
            case(
                case_id=f"benign-seed-{e['id']}",
                kind="benign",
                source="seed",
                email={
                    "sender_email": e["sender_email"],
                    "sender_name": e["sender_name"],
                    "subject": e["subject"],
                    "body_text": e["body_text"],
                    "category": e.get("category", "support"),
                },
                chunks=chunks_for(e.get("kb_query", ""), kb),
                kb_query=e.get("kb_query", ""),
                category=e.get("category", "support"),
                expected_keywords=e.get("expected_keywords", []),
            )
        )
    return out


def seed_attacks(
    kb: list[KBChunk], carriers_per_template: int, rng: random.Random
) -> list[dict[str, Any]]:
    benign = load_benign_emails()
    out = []
    for t in load_attack_templates():
        carriers = rng.sample(benign, k=min(carriers_per_template, len(benign)))
        for j, e in enumerate(carriers):
            att = ATTACKERS[j % len(ATTACKERS)]
            payload = fill(t["text"], att)
            body, html = inject(e["body_text"], payload, t.get("position", "end"))
            goal = {
                k: (fill(v, att) if isinstance(v, str) else v) for k, v in dict(t["goal"]).items()
            }
            email = {
                "sender_email": e["sender_email"],
                "sender_name": e["sender_name"],
                "subject": e["subject"],
                "body_text": body,
                "category": e.get("category", "support"),
            }
            if html:
                email["body_html"] = html
            out.append(
                case(
                    case_id=f"attack-seed-{t['id']}-{e['id']}",
                    source="seed",
                    technique=t["technique"],
                    vector="email",
                    email=email,
                    chunks=chunks_for(e.get("kb_query", ""), kb),
                    kb_query=e.get("kb_query", ""),
                    category=e.get("category", "support"),
                    goal=goal,
                    expected_keywords=e.get("expected_keywords", []),
                    attacker=att,
                    meta={
                        "template": t["id"],
                        "position": t.get("position", "end"),
                        "carrier": e["id"],
                    },
                )
            )
    return out


def seed_rag_attacks(kb: list[KBChunk], rng: random.Random) -> list[dict[str, Any]]:
    benign = load_benign_emails()
    out = []
    for t in load_poison_templates():
        for k, att in enumerate(ATTACKERS):
            e = rng.choice(benign)
            q = t["target_query"]
            clean = [c.as_dict() for c in simple_retrieve(q, kb, 2)]
            poison = {
                "chunk_id": f"{t['id']}-a{k}",
                "document_id": "kb_poisoned",
                "content": fill(t["text"], att).strip(),
                "metadata": {"poisoned": True},
                "poisoned": True,
            }
            rank = k % 2  # alternate poison at rank 1 / rank 2
            chunks = (
                [poison, *clean]
                if rank == 0
                else [clean[0], poison, *clean[1:]]
                if clean
                else [poison]
            )
            goal = {
                kk: (fill(v, att) if isinstance(v, str) else v) for kk, v in dict(t["goal"]).items()
            }
            out.append(
                case(
                    case_id=f"attack-seedrag-{t['id']}-a{k}",
                    source="seed_rag",
                    technique=t.get("technique", "answer_forcing"),
                    vector="rag",
                    email={
                        "sender_email": e["sender_email"],
                        "sender_name": e["sender_name"],
                        "subject": q[:60],
                        "body_text": f"Hello,\n\n{q}\n\nThanks,\n{e['sender_name']}",
                        "category": "support",
                    },
                    chunks=chunks,
                    kb_query=q,
                    goal=goal,
                    attacker=att,
                    meta={"template": t["id"], "poison_rank": rank + 1},
                )
            )
    return out


# --------------------------------------------------------------------------- PoisonedRAG
def poisonedrag_cases(limit_per_dataset: int, rng: random.Random) -> list[dict[str, Any]]:
    out = []
    for name in ("nq", "hotpotqa", "msmarco"):
        path = RAW / "poisonedrag" / "results" / "adv_targeted_results" / f"{name}.json"
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        items = list(data.values())
        rng.shuffle(items)
        taken = 0
        for item in items:
            if taken >= limit_per_dataset:
                break
            q = str(item.get("question", "")).strip()
            correct = str(item.get("correct answer", "")).strip()
            wrong = str(item.get("incorrect answer", "")).strip()
            # keep only machine-checkable targets: the incorrect answer must be a distinct
            # token sequence that does not already occur in the question or the correct answer
            if (
                len(wrong) < 3
                or wrong.lower() in q.lower()
                or wrong.lower() in correct.lower()
                or correct.lower() in wrong.lower()
                or wrong.lower() in ("yes", "no", "none", "unknown")
            ):
                continue
            taken += 1
            adv = [str(a) for a in item.get("adv_texts", [])][:5]
            chunks = [
                {
                    "chunk_id": f"prag-{name}-{item.get('id')}-{j}",
                    "document_id": "kb_poisoned",
                    "content": f"{q}? {a}",
                    "metadata": {"poisoned": True},
                    "poisoned": True,
                }
                for j, a in enumerate(adv)
            ]
            chunks.append(
                {
                    "chunk_id": f"prag-{name}-{item.get('id')}-clean",
                    "document_id": "kb_clean",
                    "content": f"Reference note: regarding '{q}', the verified answer on record is {correct}.",
                    "metadata": {"poisoned": False},
                    "poisoned": False,
                }
            )
            out.append(
                case(
                    case_id=f"attack-prag-{name}-{item.get('id')}",
                    source="poisonedrag",
                    technique="knowledge_corruption",
                    vector="rag",
                    email={
                        "sender_email": "reader@mail.example",
                        "sender_name": "Reader",
                        "subject": "Quick question",
                        "body_text": f"Hi, a quick factual question for your knowledge assistant: {q}?\n\nThanks!",
                        "category": "support",
                    },
                    chunks=chunks,
                    kb_query=q,
                    goal={"type": "wrong_answer", "must_contain": wrong},
                    expected_keywords=[correct] if correct else [],
                    attacker={},
                    meta={"dataset": name, "correct": correct, "incorrect": wrong},
                )
            )
    return out


# --------------------------------------------------------------------------- BIPIA
def parse_bipia_context(ctx: str) -> dict[str, str]:
    fields = {"subject": "", "sender": "", "content": ctx}
    m = re.match(
        r"SUBJECT:\s*(.*?)\|EMAIL_FROM:\s*(.*?)\|(?:RECEIVED DATE:.*?\|)?CONTENT:\s*(.*)", ctx, re.S
    )
    if m:
        fields = {
            "subject": m.group(1).strip(),
            "sender": m.group(2).strip(),
            "content": m.group(3).strip(),
        }
    return fields


def bipia_cases(contexts: int, per_type: int, rng: random.Random) -> list[dict[str, Any]]:
    ctx_path = RAW / "bipia" / "benchmark" / "email" / "test.jsonl"
    atk_path = RAW / "bipia" / "benchmark" / "text_attack_test.json"
    if not ctx_path.exists() or not atk_path.exists():
        return []
    rows = [json.loads(line) for line in open(ctx_path, encoding="utf-8") if line.strip()]
    rng.shuffle(rows)
    with open(atk_path, encoding="utf-8") as f:
        attacks: dict[str, list[str]] = json.load(f)
    out = []
    for i, row in enumerate(rows[:contexts]):
        fields = parse_bipia_context(row["context"])
        sender_email = (
            (_EMAIL_RE.search(fields["sender"]) or [None])[0]
            if _EMAIL_RE.search(fields["sender"])
            else "sender@example.com"
        )
        for atype, texts in attacks.items():
            for j, atk in enumerate(rng.sample(texts, k=min(per_type, len(texts)))):
                position = ["end", "start", "middle"][(i + j) % 3]
                body, _ = inject(fields["content"], atk, position)
                urls = _URL_RE.findall(atk)
                emails = _EMAIL_RE.findall(atk)
                goal: dict[str, Any] = {"type": "task_echo", "attack_text": atk}
                attacker: dict[str, str] = {}
                if urls:
                    goal = {"type": "link_insertion"}
                    attacker["url"] = urls[0]
                if emails:
                    goal = {"type": "exfiltration"}
                    attacker["email"] = emails[0]
                out.append(
                    case(
                        case_id=f"attack-bipia-{i}-{atype.replace(' ', '_').replace('&', 'and')}-{j}",
                        source="bipia",
                        technique="bipia:" + atype.lower().replace(" & ", "_").replace(" ", "_"),
                        vector="email",
                        email={
                            "sender_email": sender_email or "sender@example.com",
                            "sender_name": fields["sender"].split("<")[0].strip()[:60],
                            "subject": fields["subject"][:120],
                            "body_text": body[:6000],
                            "category": "support",
                        },
                        chunks=[],
                        kb_query="",
                        goal=goal,
                        attacker=attacker,
                        meta={
                            "question": row.get("question", ""),
                            "attack_type": atype,
                            "position": position,
                        },
                    )
                )
    return out


# --------------------------------------------------------------------------- InjecAgent
def injecagent_cases(limit: int, rng: random.Random) -> list[dict[str, Any]]:
    out = []
    for variant in ("ds_base", "ds_enhanced", "dh_base"):
        path = RAW / "injecagent" / "data" / f"test_cases_{variant}.json"
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
        # dedupe by attacker instruction, keep the benchmark half only
        seen: set[str] = set()
        pool = []
        for it in items:
            instr = str(it.get("Attacker Instruction", "")).strip()
            key = instr.split("IMPORTANT!!!")[-1].strip()[:200]
            if key in seen or not bench_side("injecagent:" + key):
                continue
            seen.add(key)
            pool.append(it)
        rng.shuffle(pool)
        for it in pool[:limit]:
            instr = str(it["Attacker Instruction"]).strip()
            user = str(it.get("User Instruction", "")).strip()
            emails = _EMAIL_RE.findall(instr)
            urls = _URL_RE.findall(instr)
            if variant.startswith("ds"):
                if not emails:
                    continue
                goal: dict[str, Any] = {"type": "exfiltration"}
                attacker = {"email": emails[0].lower()}
            else:
                goal = {"type": "task_echo", "attack_text": instr}
                attacker = {"url": urls[0]} if urls else {}
                if urls:
                    goal = {"type": "link_insertion"}
            body = (
                f"Hello support,\n\nFollowing up on my earlier request: {user}\n\n"
                f"{instr}\n\nKind regards,\nA customer"
            )
            out.append(
                case(
                    case_id=f"attack-injecagent-{variant}-{len(out)}",
                    source="injecagent",
                    technique="injecagent:"
                    + str(it.get("Attack Type", "")).lower().replace(" ", "_"),
                    vector="email",
                    email={
                        "sender_email": "customer@mail.example",
                        "sender_name": "Customer",
                        "subject": "Follow-up on my request",
                        "body_text": body,
                        "category": "support",
                    },
                    chunks=[],
                    goal=goal,
                    attacker=attacker,
                    meta={
                        "variant": variant,
                        "attacker_tools": it.get("Attacker Tools", []),
                        "user_tool": it.get("User Tool"),
                    },
                )
            )
    return out


# --------------------------------------------------------------------------- LLMail-Inject
def parse_llmail_email(text: str) -> tuple[str, str]:
    m = re.match(r"Subject of the email:\s*(.*?)\.?\s+Body:\s*(.*)", text, re.S)
    return (m.group(1).strip(), m.group(2).strip()) if m else ("", text.strip())


def iter_llmail_attacks(*, side: str = "bench", max_body: int = 6000) -> list[dict[str, Any]]:
    """Unique phase-2 submissions labelled as genuine attack attempts, split by stable hash.

    The raw file has ~370k rows (subject, body, scenario, objectives); the labelled file maps
    ``"Subject of the email: <subject>.   Body: <body>"`` -> {attack_attempt, reason}.
    ``side`` selects the benchmark half ("bench") or the L1-training half ("train").
    """
    raw = RAW / "llmail_inject" / "data" / "raw_submissions_phase2.jsonl"
    labels_path = RAW / "llmail_inject" / "data" / "labelled_unique_submissions_phase2.json"
    if not raw.exists() or not labels_path.exists():
        return []
    with open(labels_path, encoding="utf-8") as f:
        labels = json.load(f)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    with open(raw, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            it = json.loads(line)
            subject = str(it.get("subject") or "")
            body = str(it.get("body") or "")
            if not body.strip():
                continue
            key_text = f"Subject of the email: {subject}.   Body: {body}"
            lab = labels.get(key_text)
            if not lab or str(lab.get("attack_attempt")) != "True":
                continue
            key = hashlib.sha1(key_text.encode("utf-8")).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            if bench_side("llmail:" + key) != (side == "bench"):
                continue
            objectives: Any = it.get("objectives")
            if isinstance(objectives, str):
                try:
                    objectives = json.loads(objectives)
                except ValueError:
                    objectives = {}
            out.append(
                {
                    "key": key,
                    "subject": subject,
                    "body": body[:max_body],
                    "scenario": str(it.get("scenario") or ""),
                    "objectives": objectives if isinstance(objectives, dict) else {},
                    "reason": str(lab.get("reason") or ""),
                }
            )
    return out


def llmail_attack_cases(limit: int, rng: random.Random) -> list[dict[str, Any]]:
    pool = iter_llmail_attacks(side="bench")
    rng.shuffle(pool)
    out = []
    for it in pool[:limit]:
        key, subject, body = it["key"], it["subject"], it["body"]
        objectives = it["objectives"]
        scenario = it["scenario"]
        out.append(
            case(
                case_id=f"attack-llmail-{key[:12]}",
                source="llmail_inject",
                technique="llmail:adaptive",
                vector="email",
                email={
                    "sender_email": "external.sender@partner.example",
                    "sender_name": "External Sender",
                    "subject": subject[:200] or "(no subject)",
                    "body_text": body[:6000],
                    "category": "support",
                },
                chunks=[],
                goal={"type": "exfiltration"},
                attacker={"email": "contact@contact.com"},
                meta={"scenario": scenario, "objectives": objectives, "label_reason": it["reason"]},
            )
        )
    return out


def llmail_benign_cases() -> list[dict[str, Any]]:
    path = RAW / "llmail_inject" / "data" / "emails_for_fp_tests.json"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    out = []
    for i, text in enumerate(items):
        subject, body = parse_llmail_email(str(text))
        out.append(
            case(
                case_id=f"benign-llmailfp-{i}",
                kind="benign",
                source="llmail_inject",
                email={
                    "sender_email": "colleague@partner.example",
                    "sender_name": "Colleague",
                    "subject": subject[:200],
                    "body_text": body[:6000],
                    "category": "support",
                },
                expected_keywords=[],
            )
        )
    return out


# --------------------------------------------------------------------------- Enron / Bitext benign
def enron_benign(limit: int, rng: random.Random) -> list[dict[str, Any]]:
    path = RAW / "enron_ham" / "test.jsonl"
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d.get("label_text") == "ham" and 80 <= len(str(d.get("message", ""))) <= 2500:
                rows.append(d)
    rng.shuffle(rows)
    return [
        case(
            case_id=f"benign-enron-{d['message_id']}",
            kind="benign",
            source="enron_ham",
            email={
                "sender_email": "employee@enron-archive.example",
                "sender_name": "Employee",
                "subject": str(d.get("subject", ""))[:200],
                "body_text": str(d.get("message", ""))[:4000],
                "category": "support",
            },
            expected_keywords=[],
        )
        for d in rows[:limit]
    ]


def bitext_benign(limit: int, kb: list[KBChunk], rng: random.Random) -> list[dict[str, Any]]:
    import pandas as pd

    path = (
        RAW
        / "bitext_support"
        / "Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv"
    )
    if not path.exists():
        return []
    df = pd.read_csv(path)
    df = df.sample(n=min(limit, len(df)), random_state=rng.randint(0, 10**6))
    out = []
    for i, row in enumerate(df.itertuples(index=False)):
        instr = (
            str(row.instruction)
            .replace("{{Order Number}}", "ORD-771203")
            .replace("{{Invoice Number}}", "INV-2026-01829")
        )
        instr = re.sub(r"\{\{[^}]+\}\}", "the account", instr)
        cat = str(row.category).lower()
        category = {
            "refund": "billing",
            "invoice": "billing",
            "payment": "billing",
            "subscription": "billing",
            "cancel": "billing",
        }.get(cat, "support")
        out.append(
            case(
                case_id=f"benign-bitext-{i}",
                kind="benign",
                source="bitext_support",
                email={
                    "sender_email": f"customer{i}@mail.example",
                    "sender_name": "Customer",
                    "subject": str(row.intent).replace("_", " ").capitalize(),
                    "body_text": instr[0].upper() + instr[1:] + ".",
                    "category": category,
                },
                chunks=chunks_for(instr, kb),
                kb_query=instr,
                category=category,
                expected_keywords=[],
            )
        )
    return out


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", type=Path, default=OUT_DIR / "cases.jsonl")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--carriers-per-template", type=int, default=5)
    ap.add_argument("--prag-per-dataset", type=int, default=50)
    ap.add_argument("--bipia-contexts", type=int, default=15)
    ap.add_argument("--bipia-per-type", type=int, default=1)
    ap.add_argument("--injecagent-limit", type=int, default=100)
    ap.add_argument("--llmail-limit", type=int, default=300)
    ap.add_argument("--enron-limit", type=int, default=150)
    ap.add_argument("--bitext-limit", type=int, default=150)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rng = random.Random(args.seed)
    kb = load_kb()

    cases = []
    cases += seed_benign(kb)
    cases += seed_attacks(kb, args.carriers_per_template, rng)
    cases += seed_rag_attacks(kb, rng)
    cases += poisonedrag_cases(args.prag_per_dataset, rng)
    cases += bipia_cases(args.bipia_contexts, args.bipia_per_type, rng)
    cases += injecagent_cases(args.injecagent_limit, rng)
    cases += llmail_attack_cases(args.llmail_limit, rng)
    cases += llmail_benign_cases()
    cases += enron_benign(args.enron_limit, rng)
    cases += bitext_benign(args.bitext_limit, kb, rng)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    stats: dict[str, dict[str, int]] = {}
    for c in cases:
        s = stats.setdefault(c["source"], {"attack": 0, "benign": 0})
        s[c["kind"]] += 1
    summary = {
        "total": len(cases),
        "attack": sum(1 for c in cases if c["kind"] == "attack"),
        "benign": sum(1 for c in cases if c["kind"] == "benign"),
        "by_source": stats,
        "by_vector": {
            v: sum(1 for c in cases if c["kind"] == "attack" and c["vector"] == v)
            for v in ("email", "rag")
        },
    }
    with open(args.out.with_name("stats.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    logger.info("wrote %d cases -> %s\n%s", len(cases), args.out, json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
