"""Download the registered public datasets into ``datasets/raw/<source>/``.

    python -m mailguard.datasets.download --all
    python -m mailguard.datasets.download --source bipia --source injecagent
    python -m mailguard.datasets.download --manifest        # print provenance table

Every fetched file is recorded in ``datasets/raw/MANIFEST.json`` with size, sha256,
origin URL, license and timestamp so experiments are reproducible.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from mailguard.config.settings import PROJECT_ROOT
from mailguard.datasets.sources import SOURCES, DatasetSource, manifest_rows

logger = logging.getLogger("mailguard.datasets.download")

RAW_DIR = PROJECT_ROOT / "datasets" / "raw"
MANIFEST = RAW_DIR / "MANIFEST.json"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_manifest() -> dict[str, dict]:
    if MANIFEST.exists():
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_manifest(m: dict[str, dict]) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, ensure_ascii=False)


def _record(
    manifest: dict[str, dict], src: DatasetSource, rel: str, path: Path, origin: str
) -> None:
    manifest[f"{src.name}/{rel}"] = {
        "source": src.name,
        "file": rel,
        "path": str(path.relative_to(PROJECT_ROOT)),
        "bytes": path.stat().st_size,
        "sha256": sha256_of(path),
        "origin": origin,
        "license": src.license,
        "citation": src.citation,
        "fetched_at": datetime.now(UTC).isoformat(),
    }


def download_github(src: DatasetSource, manifest: dict[str, dict], force: bool) -> int:
    n = 0
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        for rel in src.files:
            url = f"https://raw.githubusercontent.com/{src.ref}/{src.branch}/{rel}"
            dest = RAW_DIR / src.name / rel
            if dest.exists() and not force:
                logger.info("skip (exists) %s", dest)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            logger.info("GET %s", url)
            with client.stream("GET", url) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
            _record(manifest, src, rel, dest, url)
            n += 1
    return n


def _hf_matches(repo_files: list[str], pattern: str) -> list[str]:
    if pattern.endswith("*"):
        return [f for f in repo_files if fnmatch.fnmatch(f, pattern)]
    return [pattern] if pattern in repo_files else []


def download_hf(src: DatasetSource, manifest: dict[str, dict], force: bool, max_mb: float) -> int:
    from huggingface_hub import HfApi, hf_hub_download

    api = HfApi()
    info = api.dataset_info(src.ref, files_metadata=True)
    sizes = {s.rfilename: (s.size or 0) for s in info.siblings}
    repo_files = list(sizes)
    n = 0
    for pattern in src.files:
        matches = _hf_matches(repo_files, pattern)
        if not matches:
            logger.warning("%s: no file matches %s", src.ref, pattern)
        for rel in matches:
            size_mb = sizes.get(rel, 0) / 1e6
            if size_mb > max_mb:
                logger.warning(
                    "skip %s/%s (%.0f MB > --max-mb %.0f)", src.ref, rel, size_mb, max_mb
                )
                continue
            dest = RAW_DIR / src.name / rel
            if dest.exists() and not force:
                logger.info("skip (exists) %s", dest)
                continue
            logger.info("hf_hub_download %s/%s (%.1f MB)", src.ref, rel, size_mb)
            local = hf_hub_download(
                repo_id=src.ref,
                filename=rel,
                repo_type="dataset",
                local_dir=str(RAW_DIR / src.name),
                force_download=force,
            )
            path = Path(local)
            _record(
                manifest,
                src,
                rel,
                path,
                f"https://huggingface.co/datasets/{src.ref}/resolve/main/{rel}",
            )
            n += 1
    return n


def download(names: list[str], *, force: bool = False, max_mb: float = 500.0) -> dict[str, int]:
    manifest = load_manifest()
    counts: dict[str, int] = {}
    for name in names:
        src = SOURCES[name]
        try:
            if src.kind == "github":
                counts[name] = download_github(src, manifest, force)
            else:
                counts[name] = download_hf(src, manifest, force, max_mb)
        except Exception as exc:  # keep going; report at the end
            logger.error("%s failed: %s", name, exc)
            counts[name] = -1
        save_manifest(manifest)
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--all", action="store_true", help="download every registered source")
    ap.add_argument("--source", action="append", default=[], help="source name (repeatable)")
    ap.add_argument("--role", action="append", default=[], help="download sources with this role")
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--max-mb", type=float, default=500.0, help="skip single files larger than this"
    )
    ap.add_argument("--manifest", action="store_true", help="print the provenance table and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s"
    )

    if args.manifest:
        rows = manifest_rows()
        print("| name | kind | ref | license | roles |")
        print("|---|---|---|---|---|")
        for r in rows:
            print(f"| {r['name']} | {r['kind']} | {r['ref']} | {r['license']} | {r['roles']} |")
        return 0

    names = list(args.source)
    if args.all:
        names = list(SOURCES)
    for role in args.role:
        names += [s.name for s in SOURCES.values() if role in s.roles and s.name not in names]
    if not names:
        ap.error("nothing selected: use --all, --source or --role")
    unknown = [n for n in names if n not in SOURCES]
    if unknown:
        ap.error(f"unknown sources: {unknown}; known: {sorted(SOURCES)}")
    counts = download(names, force=args.force, max_mb=args.max_mb)
    for name, c in counts.items():
        print(f"{name:28s} {'FAILED' if c < 0 else f'{c} file(s) fetched'}")
    return 1 if any(c < 0 for c in counts.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
