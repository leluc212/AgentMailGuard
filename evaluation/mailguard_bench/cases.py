"""Pinned case set for the AgentMailGuard benchmark (task 7.19; spec §3; R22.12).

Pure functions with no ``mailguard`` import, so CI tests them on synthetic pools. The real
pools come from AgentMailGuard's own builder (``build_cases.load_pools``), which keeps the
LLMail attacks on the benchmark half of the guard's sha1 split
(``iter_llmail_attacks(side="bench")``).

Selection (seed 20260930, each draw on its own derived seed):
  llmail_attack   300 LLMail-Inject attacks, stratified by meta["scenario"]
  llmail_benign   150 of the 203 LLMail-Inject false-positive-test emails
  rag_attack      100 RAG-vector attacks, stratified by source and PoisonedRAG dataset
  ablation_attack 100 of the 300 llmail_attack cases, stratified by scenario (C1/C2 runs,
                  which reuse llmail_benign as their benign set)
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from math import floor
from pathlib import Path
from typing import Any

from evaluation.mailguard_bench.guard_env import REPO_ROOT

SEED = 20260930
N_LLMAIL_ATTACK = 300
N_LLMAIL_BENIGN = 150
N_RAG_ATTACK = 100
N_ABLATION_ATTACK = 100
MANIFEST_SCHEMA = "mailguard-case-manifest/v1"
CASES_FILE = "cases.jsonl"
MANIFEST_FILE = "manifest.json"
DEFAULT_CASE_DIR = REPO_ROOT / "evaluation" / "datasets" / "mailguard"
SET_NAMES = ("llmail_attack", "llmail_benign", "rag_attack", "ablation_attack")
REQUIRED_KEYS = ("case_id", "kind", "source", "vector", "email", "goal", "attacker", "meta")

CaseDict = dict[str, Any]


class CaseManifestError(ValueError):
    """A pool, selection, manifest or case file breaks the pinned-case contract."""


def derive_seed(seed: int, label: str) -> int:
    """A stable per-draw seed, independent of PYTHONHASHSEED and of draw order."""
    return int(hashlib.sha256(f"{seed}:{label}".encode()).hexdigest()[:16], 16)


def allocate(counts: Mapping[str, int], n: int) -> dict[str, int]:
    """Split ``n`` draws across strata in proportion to their sizes.

    Every non-empty stratum gets one draw first when ``n`` allows it, the rest is shared in
    proportion to the remaining capacity, and leftover draws go to the largest fractional
    remainders (ties by stratum name). Exact arithmetic, so the result is reproducible.
    """
    strata = sorted(k for k, v in counts.items() if v > 0)
    total = sum(counts[k] for k in strata)
    if n < 0 or n > total:
        raise CaseManifestError(f"cannot draw {n} cases from a pool of {total}")
    floor_one = n >= len(strata)
    base = dict.fromkeys(strata, 1 if floor_one else 0)
    capacity = {k: counts[k] - base[k] for k in strata}
    remaining = n - sum(base.values())
    cap_total = sum(capacity.values())
    quotas = {
        k: (Fraction(remaining * capacity[k], cap_total) if cap_total else Fraction(0))
        for k in strata
    }
    alloc = {k: base[k] + floor(quotas[k]) for k in strata}
    left = n - sum(alloc.values())
    by_remainder = sorted(strata, key=lambda k: (-(quotas[k] - floor(quotas[k])), k))
    for k in by_remainder[:left]:
        alloc[k] += 1
    return alloc


def _case_id(case: CaseDict) -> str:
    return str(case["case_id"])


def stratified_sample(
    cases: Sequence[CaseDict], n: int, *, key: Callable[[CaseDict], str], seed: int
) -> list[CaseDict]:
    """Draw ``n`` cases, ``allocate``-d across ``key`` strata, sorted by case_id.

    The input order does not matter: strata and their members are sorted before drawing.
    """
    groups: dict[str, list[CaseDict]] = {}
    for case in sorted(cases, key=_case_id):
        groups.setdefault(key(case), []).append(case)
    alloc = allocate({k: len(v) for k, v in groups.items()}, n)
    rng = random.Random(seed)
    picked: list[CaseDict] = []
    for stratum in sorted(groups):
        picked.extend(rng.sample(groups[stratum], alloc.get(stratum, 0)))
    return sorted(picked, key=_case_id)


def llmail_scenario(case: CaseDict) -> str:
    """The LLMail-Inject scenario (e.g. ``level2v``) the builder keeps in meta."""
    return str((case.get("meta") or {}).get("scenario") or "unknown")


def rag_stratum(case: CaseDict) -> str:
    """``poisonedrag:<dataset>`` for PoisonedRAG cases, the source name otherwise."""
    dataset = str((case.get("meta") or {}).get("dataset") or "")
    return f"{case['source']}:{dataset}" if dataset else str(case["source"])


def benign_stratum(case: CaseDict) -> str:
    """Benign emails carry no scenario; one stratum per source."""
    return str(case["source"])


def canonical_line(case: CaseDict) -> str:
    """The one serialisation used both for cases.jsonl and for the manifest hash."""
    return json.dumps(case, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def cases_sha256(cases: Sequence[CaseDict]) -> str:
    """sha256 of the canonical case lines sorted by case_id (= sha256 of cases.jsonl)."""
    body = "".join(canonical_line(c) + "\n" for c in sorted(cases, key=_case_id))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def validate_pool(
    name: str, cases: Sequence[CaseDict], *, kind: str, vector: str, source: str | None = None
) -> None:
    """Fail on missing keys, duplicate ids, or a case of the wrong kind/vector/source."""
    seen: set[str] = set()
    for case in cases:
        missing = [k for k in REQUIRED_KEYS if k not in case]
        if missing:
            raise CaseManifestError(f"{name}: case {case.get('case_id')!r} lacks {missing}")
        cid = _case_id(case)
        if cid in seen:
            raise CaseManifestError(f"{name}: duplicate case_id {cid}")
        seen.add(cid)
        wrong_source = source is not None and case["source"] != source
        if case["kind"] != kind or case["vector"] != vector or wrong_source:
            raise CaseManifestError(
                f"{name}: case {cid} is kind={case['kind']} vector={case['vector']} "
                f"source={case['source']}, expected kind={kind} vector={vector} "
                f"source={source or 'any'}"
            )


@dataclass(frozen=True)
class CasePools:
    """Everything the guard's builder offers, before sampling."""

    llmail_attack: list[CaseDict]
    llmail_benign: list[CaseDict]
    rag_attack: list[CaseDict]


@dataclass(frozen=True)
class CaseSelection:
    """The sampled sets. ablation_attack is a subset of llmail_attack."""

    llmail_attack: list[CaseDict]
    llmail_benign: list[CaseDict]
    rag_attack: list[CaseDict]
    ablation_attack: list[CaseDict]

    def sets(self) -> dict[str, list[CaseDict]]:
        """The four sets by name, in SET_NAMES order."""
        return {name: list(getattr(self, name)) for name in SET_NAMES}

    def all_cases(self) -> list[CaseDict]:
        """Every distinct selected case, sorted by case_id."""
        unique: dict[str, CaseDict] = {}
        for cases in self.sets().values():
            for case in cases:
                unique[_case_id(case)] = case
        return [unique[cid] for cid in sorted(unique)]


def select_cases(
    pools: CasePools,
    *,
    seed: int = SEED,
    n_attack: int = N_LLMAIL_ATTACK,
    n_benign: int = N_LLMAIL_BENIGN,
    n_rag: int = N_RAG_ATTACK,
    n_ablation: int = N_ABLATION_ATTACK,
) -> CaseSelection:
    """Validate the pools and draw the four sets. A pool that is too small fails loudly."""
    validate_pool(
        "llmail_attack", pools.llmail_attack, kind="attack", vector="email", source="llmail_inject"
    )
    validate_pool(
        "llmail_benign", pools.llmail_benign, kind="benign", vector="email", source="llmail_inject"
    )
    validate_pool("rag_attack", pools.rag_attack, kind="attack", vector="rag")
    if n_ablation > n_attack:
        raise CaseManifestError(f"ablation subset {n_ablation} > LLMail sample {n_attack}")
    attacks = stratified_sample(
        pools.llmail_attack, n_attack, key=llmail_scenario, seed=derive_seed(seed, "llmail_attack")
    )
    benign = stratified_sample(
        pools.llmail_benign, n_benign, key=benign_stratum, seed=derive_seed(seed, "llmail_benign")
    )
    rag = stratified_sample(
        pools.rag_attack, n_rag, key=rag_stratum, seed=derive_seed(seed, "rag_attack")
    )
    ablation = stratified_sample(
        attacks, n_ablation, key=llmail_scenario, seed=derive_seed(seed, "ablation_attack")
    )
    return CaseSelection(
        llmail_attack=attacks, llmail_benign=benign, rag_attack=rag, ablation_attack=ablation
    )


def _counts(cases: Sequence[CaseDict], key: Callable[[CaseDict], str]) -> dict[str, int]:
    return dict(sorted(Counter(key(c) for c in cases).items()))


def build_manifest(
    selection: CaseSelection, pools: CasePools, *, seed: int, provenance: Mapping[str, str]
) -> dict[str, Any]:
    """The committed manifest: ids per set, strata, pool sizes, hash, provenance.

    No timestamp, so rebuilding from the same raw data gives a byte-identical file.
    """
    sets = selection.sets()
    return {
        "schema": MANIFEST_SCHEMA,
        "task": "7.19",
        "seed": seed,
        "cases_file": CASES_FILE,
        "cases_sha256": cases_sha256(selection.all_cases()),
        "counts": {name: len(cases) for name, cases in sets.items()},
        "pool_sizes": {
            "llmail_attack": len(pools.llmail_attack),
            "llmail_benign": len(pools.llmail_benign),
            "rag_attack": len(pools.rag_attack),
        },
        "strata": {
            "llmail_attack_pool": _counts(pools.llmail_attack, llmail_scenario),
            "llmail_attack": _counts(selection.llmail_attack, llmail_scenario),
            "ablation_attack": _counts(selection.ablation_attack, llmail_scenario),
            "rag_attack_pool": _counts(pools.rag_attack, rag_stratum),
            "rag_attack": _counts(selection.rag_attack, rag_stratum),
        },
        "ablation_benign_set": "llmail_benign",
        "sets": {name: [_case_id(c) for c in cases] for name, cases in sets.items()},
        "provenance": dict(sorted(provenance.items())),
    }


def reconcile(existing: Mapping[str, Any] | None, fresh: Mapping[str, Any], *, force: bool) -> None:
    """Refuse to silently re-pin: a rebuild must reproduce the committed ids and hash."""
    if existing is None or force:
        return
    same_sets = existing.get("sets") == fresh.get("sets")
    same_hash = existing.get("cases_sha256") == fresh.get("cases_sha256")
    if not (same_sets and same_hash):
        raise CaseManifestError(
            "rebuilt case set differs from the committed manifest (different raw data or "
            "AgentMailGuard commit?); refusing to overwrite. Compare 'provenance', or pass "
            "--force to re-pin deliberately"
        )


def write_case_set(
    selection: CaseSelection, manifest: Mapping[str, Any], out_dir: Path
) -> tuple[Path, Path]:
    """Write cases.jsonl (canonical lines, sorted by id) and manifest.json."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cases_path = out_dir / CASES_FILE
    manifest_path = out_dir / MANIFEST_FILE
    body = "".join(canonical_line(c) + "\n" for c in selection.all_cases())
    cases_path.write_text(body, encoding="utf-8")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return cases_path, manifest_path


@dataclass(frozen=True)
class LoadedCaseSet:
    """A verified manifest plus its cases by id."""

    manifest: dict[str, Any]
    cases: dict[str, CaseDict]

    def cases_in(self, set_name: str) -> list[CaseDict]:
        """The cases of one set, in manifest order."""
        sets: dict[str, list[str]] = self.manifest["sets"]
        if set_name not in sets:
            raise CaseManifestError(f"unknown case set {set_name!r}; known: {sorted(sets)}")
        return [self.cases[cid] for cid in sets[set_name]]


def load_case_set(case_dir: Path = DEFAULT_CASE_DIR) -> LoadedCaseSet:
    """Load manifest.json and cases.jsonl and verify schema, hash and id coverage."""
    manifest_path = case_dir / MANIFEST_FILE
    if not manifest_path.is_file():
        raise CaseManifestError(f"{manifest_path} missing; run `make mailguard-cases`")
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise CaseManifestError(f"{manifest_path}: schema {manifest.get('schema')!r} unsupported")
    cases_path = case_dir / str(manifest["cases_file"])
    if not cases_path.is_file():
        raise CaseManifestError(
            f"{cases_path} missing; rebuild it with `make mailguard-cases` "
            "(the manifest pins its hash)"
        )
    cases: dict[str, CaseDict] = {}
    # Split on "\n" only: str.splitlines() also breaks on U+0085/U+2028/..., which
    # json.dumps(ensure_ascii=False) leaves unescaped inside real LLMail bodies.
    for line in cases_path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        case: CaseDict = json.loads(line)
        cid = _case_id(case)
        if cid in cases:
            raise CaseManifestError(f"{cases_path}: duplicate case_id {cid}")
        cases[cid] = case
    digest = cases_sha256(list(cases.values()))
    if digest != manifest["cases_sha256"]:
        raise CaseManifestError(
            f"{cases_path} hash {digest} != manifest {manifest['cases_sha256']}; "
            "the case file is not the pinned one"
        )
    listed = {cid for ids in manifest["sets"].values() for cid in ids}
    if listed != set(cases):
        raise CaseManifestError(
            f"{cases_path}: ids do not match the manifest sets "
            f"({len(listed - set(cases))} listed but absent, "
            f"{len(set(cases) - listed)} present but unlisted)"
        )
    return LoadedCaseSet(manifest=manifest, cases=cases)
