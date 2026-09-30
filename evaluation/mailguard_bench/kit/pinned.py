"""The pinned benchmark inputs and their verification (task 7.25; R22.12).

The teammate does not rebuild the 550 benchmark cases (``make mailguard-cases``) or train the L1
classifier (``make mailguard-prep``): both are pinned here by sha256. The case set and its
companions are committed. The classifier is **not**: ADR-0012 decision 15 keeps it out of git
(42 % of its training rows come from a dataset that declares no license), so the owner sends the
file privately and the teammate puts it in ``pinned/``. ``verify`` names everything wrong with a
checkout, a missing or different classifier included; ``check_scikit_learn`` covers the one thing
a byte-identical classifier still depends on, the scikit-learn version it was pickled with.

This module never imports the guard or scikit-learn (CI imports it and does not install the guard).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from evaluation.mailguard_bench.guard_env import REPO_ROOT, sha256_file

SKLEARN_VERSION = "1.9.1"
"""The scikit-learn the L1 classifier was built with; pickles do not load across versions."""


@dataclass(frozen=True)
class PinnedFile:
    """A file of the repository and the sha256 its bytes must have."""

    path: Path
    sha256: str


@dataclass(frozen=True)
class PinnedSpec:
    """Where the pinned inputs are (relative to the repository root) and what they must hash to."""

    cases: PinnedFile
    manifest: Path
    classifier: PinnedFile
    metrics: Path
    sums: Path


PINNED_DIR = Path("evaluation/mailguard_bench/pinned")
PINNED = PinnedSpec(
    cases=PinnedFile(
        Path("evaluation/datasets/mailguard/cases.jsonl"),
        "c00dddca6336df91bcf80de7904ad5a8335564ababd8618c7b1d953a23811d19",
    ),
    manifest=Path("evaluation/datasets/mailguard/manifest.json"),
    classifier=PinnedFile(
        PINNED_DIR / "l1_injection_clf_v1.joblib",
        "8fc1cbe74a599ab870a10ca5ff43f4a6d80b3e2273e36c7ed163c637a1d40103",
    ),
    metrics=PINNED_DIR / "l1_injection_clf_v1.metrics.json",
    sums=PINNED_DIR / "SHA256SUMS",
)


def parse_sha256sums(text: str) -> dict[str, str]:
    """``{path: sha256}`` from ``sha256sum`` output (blank lines and ``#`` comments skipped)."""
    entries: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, name = line.partition(" ")
        entries[name.lstrip(" *")] = digest
    return entries


def classifier_fix(spec: PinnedSpec = PINNED) -> str:
    """What to do about a classifier that is missing or is another file (ADR-0012 decision 15)."""
    classifier = spec.classifier
    return (
        f"ask the owner for {classifier.path.name} (it is not in git, see NOTICE.md), "
        f"put it in {classifier.path.parent.as_posix()}/; its sha256 must be {classifier.sha256}"
    )


def _hash_problem(root: Path, pinned: PinnedFile, *, restore: str | None = None) -> str | None:
    """Why ``pinned`` is not what the repository pins (None when it is).

    ``restore`` is what to do about a missing or different file; the default is for files in git.
    """
    path = root / pinned.path
    fix = restore or f"it ships in git, run `git checkout -- {pinned.path}`"
    if not path.is_file():
        return f"{pinned.path} is missing; {fix}"
    found = sha256_file(path)
    if found == pinned.sha256:
        return None
    message = f"{pinned.path} has sha256 {found}, the pinned one is {pinned.sha256}"
    if restore is not None:
        return f"{message}; {restore}"
    if b"\r\n" in path.read_bytes()[: 1 << 20] and path.suffix in {".jsonl", ".json"}:
        message += (
            "; the file has CRLF line endings, so git converted it on checkout: run "
            "`git config core.autocrlf input`, delete the file and `git checkout -- "
            f"{pinned.path}`"
        )
    return message


def _sums_problems(root: Path, spec: PinnedSpec) -> list[str]:
    """Problems of the SHA256SUMS file: absent, or disagreeing with the pins or the files."""
    sums_path = root / spec.sums
    if not sums_path.is_file():
        return [f"{spec.sums} is missing"]
    entries = parse_sha256sums(sums_path.read_text(encoding="utf-8"))
    base = sums_path.parent
    problems: list[str] = []
    pinned_by_path = {
        (root / spec.cases.path).resolve(): spec.cases.sha256,
        (root / spec.classifier.path).resolve(): spec.classifier.sha256,
    }
    listed = {(base / name).resolve(): digest for name, digest in entries.items()}
    for path, expected in pinned_by_path.items():
        digest = listed.get(path)
        if digest != expected:
            problems.append(
                f"{spec.sums} lists {digest or 'nothing'} for {path.name}, "
                f"the pinned sha256 is {expected}"
            )
    classifier_path = (root / spec.classifier.path).resolve()
    for name, digest in entries.items():
        target = base / name
        if not target.is_file():
            if target.resolve() == classifier_path:
                continue  # not in git: classifier_problem reports its absence, with the fix
            problems.append(f"{spec.sums} names {name}, which is missing")
        elif sha256_file(target) != digest:
            problems.append(f"{name} does not match its line in {spec.sums}")
    return problems


def _manifest_problems(root: Path, spec: PinnedSpec) -> list[str]:
    path = root / spec.manifest
    if not path.is_file():
        return [f"{spec.manifest} is missing; it ships in git"]
    try:
        declared = json.loads(path.read_text(encoding="utf-8")).get("cases_sha256")
    except (OSError, ValueError) as exc:
        return [f"{spec.manifest} is not readable JSON: {exc}"]
    if declared != spec.cases.sha256:
        return [
            f"{spec.manifest} says cases_sha256 {declared}, "
            f"the pinned cases file is {spec.cases.sha256}"
        ]
    return []


def classifier_problem(root: Path = REPO_ROOT, spec: PinnedSpec = PINNED) -> str | None:
    """Why the owner-supplied L1 classifier is not usable (None when it is the pinned file)."""
    return _hash_problem(root, spec.classifier, restore=classifier_fix(spec))


def verify_committed(root: Path = REPO_ROOT, spec: PinnedSpec = PINNED) -> list[str]:
    """Everything wrong with the inputs that ship in git (every pinned file but the classifier)."""
    problems: list[str] = []
    cases = _hash_problem(root, spec.cases)
    if cases:
        problems.append(cases)
    if not (root / spec.metrics).is_file():
        problems.append(f"{spec.metrics} is missing; it ships in git")
    problems.extend(_manifest_problems(root, spec))
    problems.extend(_sums_problems(root, spec))
    return problems


def verify(root: Path = REPO_ROOT, spec: PinnedSpec = PINNED) -> list[str]:
    """Everything wrong with the pinned inputs of the checkout at ``root`` (empty when none)."""
    problems = verify_committed(root, spec)
    classifier = classifier_problem(root, spec)
    if classifier:
        problems.append(classifier)
    return problems


def installed_scikit_learn() -> str | None:
    """The installed scikit-learn version (None when it is not installed)."""
    try:
        return metadata.version("scikit-learn")
    except metadata.PackageNotFoundError:
        return None


def check_scikit_learn(installed: str | None, expected: str = SKLEARN_VERSION) -> str | None:
    """Why the installed scikit-learn cannot load the pinned classifier (None when it can)."""
    if installed is None:
        return f"scikit-learn is not installed; the L1 classifier needs {expected}"
    if installed != expected:
        return (
            f"scikit-learn {installed} is installed, the L1 classifier was built with {expected}; "
            "unpickling across versions can fail or change its scores"
        )
    return None


__all__ = [
    "PINNED",
    "PINNED_DIR",
    "REPO_ROOT",
    "SKLEARN_VERSION",
    "PinnedFile",
    "PinnedSpec",
    "check_scikit_learn",
    "classifier_fix",
    "classifier_problem",
    "installed_scikit_learn",
    "parse_sha256sums",
    "verify",
    "verify_committed",
]
