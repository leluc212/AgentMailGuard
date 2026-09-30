"""The pinned benchmark inputs shipped in git, and their verification (task 7.23; R22.12).

The teammate gets the 550 benchmark cases and the L1 classifier from git instead of rebuilding
them; ``verify`` says what is wrong with a checkout, and names the reason for each problem.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evaluation.mailguard_bench.kit import pinned
from evaluation.mailguard_bench.kit.pinned import (
    PinnedFile,
    PinnedSpec,
    check_scikit_learn,
    parse_sha256sums,
    verify,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _layout(root: Path, *, cases: bytes = b'{"case_id": "a"}\n') -> PinnedSpec:
    """A miniature checkout with every pinned file present and a matching spec."""
    datasets = root / "evaluation" / "datasets" / "mailguard"
    pin = root / "evaluation" / "mailguard_bench" / "pinned"
    datasets.mkdir(parents=True)
    pin.mkdir(parents=True)
    (datasets / "cases.jsonl").write_bytes(cases)
    (datasets / "manifest.json").write_text(
        json.dumps({"cases_sha256": _sha(cases)}), encoding="utf-8"
    )
    (pin / "l1.joblib").write_bytes(b"model bytes")
    (pin / "l1.metrics.json").write_text("{}", encoding="utf-8")
    sums = "\n".join(
        [
            f"{_sha(cases)}  ../../datasets/mailguard/cases.jsonl",
            f"{_sha(b'model bytes')}  l1.joblib",
            f"{_sha(b'{}')}  l1.metrics.json",
        ]
    )
    (pin / "SHA256SUMS").write_text(sums + "\n", encoding="utf-8")
    return PinnedSpec(
        cases=PinnedFile(Path("evaluation/datasets/mailguard/cases.jsonl"), _sha(cases)),
        manifest=Path("evaluation/datasets/mailguard/manifest.json"),
        classifier=PinnedFile(
            Path("evaluation/mailguard_bench/pinned/l1.joblib"), _sha(b"model bytes")
        ),
        metrics=Path("evaluation/mailguard_bench/pinned/l1.metrics.json"),
        sums=Path("evaluation/mailguard_bench/pinned/SHA256SUMS"),
    )


def test_a_complete_checkout_has_no_problems(tmp_path: Path) -> None:
    spec = _layout(tmp_path)
    assert verify(tmp_path, spec) == []


def test_a_missing_file_is_named(tmp_path: Path) -> None:
    spec = _layout(tmp_path)
    (tmp_path / spec.cases.path).unlink()
    problems = verify(tmp_path, spec)
    assert any("cases.jsonl" in p and "missing" in p for p in problems)


def test_a_changed_file_reports_both_hashes(tmp_path: Path) -> None:
    spec = _layout(tmp_path)
    (tmp_path / spec.classifier.path).write_bytes(b"other bytes")
    problems = verify(tmp_path, spec)
    [problem] = [p for p in problems if "l1.joblib" in p and "sha256" in p]
    assert spec.classifier.sha256 in problem
    assert _sha(b"other bytes") in problem


def test_a_hash_edited_in_sha256sums_alone_does_not_pass(tmp_path: Path) -> None:
    spec = _layout(tmp_path)
    sums = tmp_path / spec.sums
    sums.write_text(sums.read_text().replace(spec.classifier.sha256, "0" * 64), encoding="utf-8")
    assert any("SHA256SUMS" in p for p in verify(tmp_path, spec))


def test_a_manifest_that_disagrees_with_the_cases_file_is_a_problem(tmp_path: Path) -> None:
    spec = _layout(tmp_path)
    (tmp_path / spec.manifest).write_text(json.dumps({"cases_sha256": "1" * 64}), encoding="utf-8")
    assert any("manifest" in p and "cases_sha256" in p for p in verify(tmp_path, spec))


def test_crlf_in_the_cases_file_is_named_as_the_likely_cause(tmp_path: Path) -> None:
    spec = _layout(tmp_path)
    (tmp_path / spec.cases.path).write_bytes(b'{"case_id": "a"}\r\n')
    problems = verify(tmp_path, spec)
    assert any("CRLF" in p and "core.autocrlf" in p for p in problems)


def test_parse_sha256sums_reads_text_and_binary_marks() -> None:
    text = f"{'a' * 64}  one.txt\n{'b' * 64} *two.bin\n\n# comment\n"
    assert parse_sha256sums(text) == {"one.txt": "a" * 64, "two.bin": "b" * 64}


def test_scikit_learn_of_the_pinned_version_is_accepted() -> None:
    assert check_scikit_learn(pinned.SKLEARN_VERSION) is None


def test_another_scikit_learn_is_refused_with_the_reason() -> None:
    problem = check_scikit_learn("1.8.0")
    assert problem is not None
    assert "1.9.1" in problem and "1.8.0" in problem and "unpickl" in problem


def test_a_missing_scikit_learn_is_refused() -> None:
    problem = check_scikit_learn(None)
    assert problem is not None and "not installed" in problem


def test_the_shipped_inputs_verify() -> None:
    """The files in git are the ones the constants pin (real files, no fixture)."""
    assert verify() == []


def test_the_pinned_hashes_are_the_ones_the_context_fixed() -> None:
    assert pinned.PINNED.cases.sha256 == (
        "c00dddca6336df91bcf80de7904ad5a8335564ababd8618c7b1d953a23811d19"
    )
    assert pinned.PINNED.classifier.sha256 == (
        "8fc1cbe74a599ab870a10ca5ff43f4a6d80b3e2273e36c7ed163c637a1d40103"
    )


def test_the_installed_scikit_learn_is_the_locked_one() -> None:
    from importlib.metadata import version

    assert check_scikit_learn(version("scikit-learn")) is None


@pytest.mark.parametrize("name", ["NOTICE.md", "SHA256SUMS", "l1_injection_clf_v1.metrics.json"])
def test_the_pinned_folder_ships_its_companions(name: str) -> None:
    assert (pinned.REPO_ROOT / "evaluation" / "mailguard_bench" / "pinned" / name).is_file()


def test_shipped_files_are_not_git_ignored() -> None:
    """A teammate gets the inputs from git: none of them may be covered by .gitignore."""
    import subprocess

    shipped = [
        pinned.PINNED.cases.path,
        pinned.PINNED.manifest,
        pinned.PINNED.classifier.path,
        pinned.PINNED.metrics,
        pinned.PINNED.sums,
        pinned.PINNED_DIR / "NOTICE.md",
    ]
    result = subprocess.run(
        ["git", "check-ignore", *map(str, shipped)],
        cwd=pinned.REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, f"git-ignored: {result.stdout}"
