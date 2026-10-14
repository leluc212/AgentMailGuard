"""Capture the v2 parity golden: run every matrix case offline and write the outcome records.

python scripts/capture_guard_baseline.py                 # writes tests/golden/guard_v2_baseline.json
python scripts/capture_guard_baseline.py --out /tmp/g.json
python scripts/capture_guard_baseline.py --force         # overwrite an existing golden

The output is byte-identical across runs (see tests/unit/test_guard_parity.py).
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.parity import GOLDEN_PATH, capture, offline_settings, render_golden  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=GOLDEN_PATH, help="where to write the golden")
    ap.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = ap.parse_args(argv)
    # one "L1 ML model not found" warning per case is expected (parity runs without it)
    logging.basicConfig(level=logging.ERROR)
    out: Path = args.out
    if out.exists() and not args.force:
        print(f"{out} exists; pass --force to overwrite it", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="mailguard-parity-") as tmp:
        records = capture(offline_settings(Path(tmp)))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_golden(records), encoding="utf-8")
    print(f"wrote {len(records)} cases to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
