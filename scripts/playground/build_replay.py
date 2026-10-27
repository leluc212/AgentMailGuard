"""Build the Replay_Store of the Guard Playground (Requirements 7.9, 7.10, 7.17; D14).

Runs every Demo_Set case with every requested preset in Live_Mode and writes one
``services/guard_playground/data/replay/<case_id>__<preset>__<model_key>.json`` per pair.
A pair that fails keeps its existing file untouched; the failed pairs are printed at the end
and the exit status is 1. Every file goes through ``redact`` before it is written.

This CALLS PAID MODEL APIs. Usage (from the repo root):

    .venv\\Scripts\\python.exe scripts\\playground\\build_replay.py --dry-run
    .venv\\Scripts\\python.exe scripts\\playground\\build_replay.py --model all
    .venv\\Scripts\\python.exe scripts\\playground\\build_replay.py --model qwen --cases ID1,ID2
    .venv\\Scripts\\python.exe scripts\\playground\\build_replay.py --list-candidates

Keys come from the environment or the repo ``.env`` (BENCH_OPENAI_API_KEY,
BENCH_OPENROUTER_API_KEY); their values are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for path in (REPO, REPO / "agentmailguard"):
    if str(path) not in sys.path:
        sys.path.append(str(path))

from services.guard_playground.core.cases import load_cases  # noqa: E402
from services.guard_playground.core.models import (  # noqa: E402
    DRAFT_MODELS,
    credential_present,
    missing_credential_message,
)
from services.guard_playground.core.replay import write  # noqa: E402
from services.guard_playground.core.runner import LiveRunError, execute  # noqa: E402
from services.guard_playground.core.schema import PRESETS, ChunkInput, EmailInput  # noqa: E402
from services.guard_playground.settings import PlaygroundSettings  # noqa: E402

DEFAULT_PRESETS = ("C0", "C3")  # D14
KEY_NAMES = ("BENCH_OPENAI_API_KEY", "OPENAI_API_KEY", "BENCH_OPENROUTER_API_KEY")
RUNS = {
    "gpt-4o-mini": "2026-10-19-gpt4omini-v3-1800",
    "qwen": "2026-10-19-qwen25-7b-openrouter-v3-1800",
    "llama": "2026-10-19-llama31-8b-openrouter-v3-1800",
}


def load_dot_env(path: Path) -> None:
    """Copy the model keys from ``.env`` into this process (the environment wins)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        name = name.strip()
        if sep and name in KEY_NAMES and not os.environ.get(name):
            value = value.strip().strip('"').strip("'")
            if value:
                os.environ[name] = value


def _csv(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def _email(case: object) -> EmailInput:
    return EmailInput(
        subject=case.subject,  # type: ignore[attr-defined]
        body=case.body,  # type: ignore[attr-defined]
        chunks=[ChunkInput(chunk_id=i, content=c) for i, c in case.chunks],  # type: ignore[attr-defined]
        source="pinned" if case.origin == "pinned" else "fixture",  # type: ignore[attr-defined]
        case_id=case.case_id,  # type: ignore[attr-defined]
    )


async def build(
    settings: PlaygroundSettings,
    case_ids: Sequence[str],
    presets: Sequence[str],
    model_keys: Sequence[str],
    *,
    dry_run: bool = False,
    executor: object = execute,
) -> list[str]:
    """Run every (case, preset, model); return the failed pairs as printable strings."""
    catalog = load_cases(settings)
    out_dir = settings.data_dir / "replay"
    failed: list[str] = []
    total = len(case_ids) * len(presets) * len(model_keys)
    done = 0
    for model_key in model_keys:
        for case_id in case_ids:
            case = catalog.get(case_id)
            for preset in presets:
                done += 1
                tag = f"[{done}/{total}] {case_id} {preset} {model_key}"
                if case is None:
                    failed.append(f"{tag}: case not found")
                    print(f"{tag}  FAILED case not found", flush=True)
                    continue
                if dry_run:
                    print(f"{tag}  (dry run)", flush=True)
                    continue
                try:
                    result = await executor(  # type: ignore[operator]
                        _email(case), preset, model_key, l1_model_path=settings.l1_model_path
                    )
                    result = result.model_copy(update={"mode": "replay"})
                    target = write(result, out_dir, model_key)
                    final = result.final.action if result.final else "no decision"
                    print(f"{tag}  ok -> {final}  ({target.name})", flush=True)
                except LiveRunError as exc:
                    failed.append(f"{tag}: {exc.category}: {exc.message}")
                    print(f"{tag}  FAILED {exc.category}", flush=True)
                except Exception as exc:  # keep going; the old file stays (Req 7.10)
                    failed.append(f"{tag}: {type(exc).__name__}: {exc}"[:300])
                    print(f"{tag}  FAILED {type(exc).__name__}", flush=True)
    return failed


def list_candidates(settings: PlaygroundSettings, per_group: int = 5) -> None:
    """Cases that every model lost at C0 and blocked at C3 (attacks), or never blocked (benign),
    plus benign cases that C3 blocked on 2+ models (false positives), shortest first."""
    catalog = load_cases(settings)
    records: dict[str, tuple[dict[str, dict], dict[str, dict]]] = {}
    for key, run in RUNS.items():
        tables = []
        for cfg in ("C0", "C3"):
            rows = {}
            with (settings.results_root / run / f"ragemail__{cfg}.jsonl").open(
                encoding="utf-8"
            ) as f:
                for line in f:
                    row = json.loads(line)
                    rows[row["case_id"]] = row
            tables.append(rows)
        records[key] = (tables[0], tables[1])

    def blocked(row: dict) -> bool:
        return bool(row.get("blocked_inbound") or row.get("blocked_outbound"))

    groups: dict[str, list[tuple[int, str]]] = {}
    for case in catalog.cases:
        if case.origin != "pinned":
            continue
        rows = [(c0.get(case.case_id), c3.get(case.case_id)) for c0, c3 in records.values()]
        if any(r0 is None or r3 is None for r0, r3 in rows):
            continue
        size = len(case.body) + sum(len(c) for _i, c in case.chunks)
        if case.kind == "benign":
            hits = sum(blocked(r3) for _r0, r3 in rows)
            if hits == 0:
                groups.setdefault("benign passed (all models)", []).append((size, case.case_id))
            elif hits >= 2:
                groups.setdefault("benign blocked by C3 (2+ models)", []).append(
                    (size, case.case_id)
                )
        elif all(r0.get("goal_achieved") and blocked(r3) for r0, r3 in rows):
            groups.setdefault(f"{case.kind} {case.source}: C0 lost, C3 blocked", []).append(
                (size, case.case_id)
            )
    for name, items in sorted(groups.items()):
        print(f"{name}: {len(items)}")
        for size, case_id in sorted(items)[:per_group]:
            print(f"   {case_id}  ({size} chars)")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--model", default="gpt-4o-mini", help="gpt-4o-mini | qwen | llama | all")
    parser.add_argument("--cases", help="comma-separated case ids (default: data/demo_set.json)")
    parser.add_argument("--presets", help="comma-separated presets or 'all' (default: C0,C3)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, call nothing")
    parser.add_argument("--list-candidates", action="store_true", help="suggest Demo_Set cases")
    args = parser.parse_args(argv)

    settings = PlaygroundSettings()
    if args.list_candidates:
        list_candidates(settings)
        return 0

    keys = [m.key for m in DRAFT_MODELS]
    model_keys = keys if args.model == "all" else [args.model]
    if any(k not in keys for k in model_keys):
        parser.error(f"--model must be one of {', '.join(keys)} or all")
    presets = (
        list(PRESETS) if args.presets == "all" else (_csv(args.presets) or list(DEFAULT_PRESETS))
    )
    if any(p not in PRESETS for p in presets):
        parser.error(f"--presets must be among {', '.join(PRESETS)}")
    case_ids = _csv(args.cases)
    if case_ids is None:
        demo = settings.data_dir / "demo_set.json"
        try:
            case_ids = list(json.loads(demo.read_text(encoding="utf-8"))["cases"])
        except (OSError, ValueError, KeyError):
            parser.error(f"no --cases given and {demo} is missing or invalid")

    if not args.dry_run:
        load_dot_env(REPO / ".env")
        missing = [m for m in DRAFT_MODELS if m.key in model_keys and not credential_present(m)]
        if missing:
            for m in missing:
                print(missing_credential_message(m), file=sys.stderr)
            return 2

    print(
        f"{len(case_ids)} cases x {len(presets)} presets x {len(model_keys)} models = "
        f"{len(case_ids) * len(presets) * len(model_keys)} runs"
        + ("" if args.dry_run else "  (paid API calls)"),
        flush=True,
    )
    failed = asyncio.run(build(settings, case_ids, presets, model_keys, dry_run=args.dry_run))
    if failed:
        print(f"\n{len(failed)} failed pairs (their existing Replay files were kept):")
        for line in failed:
            print("  " + line)
        return 1
    print("\nAll pairs written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
