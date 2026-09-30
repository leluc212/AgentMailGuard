"""The pinned benchmark inputs shipped in git, and their verification (task 7.23; R22.12).

The teammate gets the 550 benchmark cases and the L1 classifier from git instead of rebuilding
them; ``verify`` says what is wrong with a checkout, and names the reason for each problem.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
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


def test_a_missing_classifier_says_to_ask_the_owner(tmp_path: Path) -> None:
    """ADR-0012 decision 15: the classifier is not in git, so `git checkout` cannot restore it."""
    spec = _layout(tmp_path)
    (tmp_path / spec.classifier.path).unlink()
    problems = verify(tmp_path, spec)
    [problem] = problems  # SHA256SUMS naming a file that is not there is the same one problem
    assert problem == (
        f"{spec.classifier.path} is missing; "
        f"ask the owner for {spec.classifier.path.name} (it is not in git, see NOTICE.md), "
        f"put it in {spec.classifier.path.parent.as_posix()}/; "
        f"its sha256 must be {spec.classifier.sha256}"
    )
    assert "git checkout" not in problem


def test_another_classifier_file_also_names_the_owner_and_both_hashes(tmp_path: Path) -> None:
    spec = _layout(tmp_path)
    (tmp_path / spec.classifier.path).write_bytes(b"retrained locally")
    [problem] = [p for p in verify(tmp_path, spec) if "sha256" in p and "l1.joblib" in p]
    assert _sha(b"retrained locally") in problem and spec.classifier.sha256 in problem
    assert "ask the owner" in problem and "git checkout" not in problem


def test_the_committed_files_verify_without_the_classifier(tmp_path: Path) -> None:
    spec = _layout(tmp_path)
    (tmp_path / spec.classifier.path).unlink()
    assert pinned.verify_committed(tmp_path, spec) == []
    (tmp_path / spec.cases.path).unlink()
    assert any("cases.jsonl" in p for p in pinned.verify_committed(tmp_path, spec))


def test_classifier_problem_is_none_only_for_the_pinned_bytes(tmp_path: Path) -> None:
    spec = _layout(tmp_path)
    assert pinned.classifier_problem(tmp_path, spec) is None
    (tmp_path / spec.classifier.path).write_bytes(b"x")
    assert pinned.classifier_problem(tmp_path, spec) is not None


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


def test_the_committed_inputs_verify() -> None:
    """The case set, manifest, metrics and SHA256SUMS in git are what the constants pin."""
    assert pinned.verify_committed() == []


_CLASSIFIER = pinned.REPO_ROOT / pinned.PINNED.classifier.path


@pytest.mark.skipif(
    not _CLASSIFIER.is_file(),
    reason="the L1 classifier is not in git (ADR-0012 decision 15); runs where the owner's copy is",
)
def test_the_owners_classifier_copy_verifies() -> None:
    assert pinned.verify() == []


def test_without_the_owners_classifier_the_real_verify_names_only_that_file() -> None:
    if _CLASSIFIER.is_file():
        pytest.skip("the classifier is here; see test_the_owners_classifier_copy_verifies")
    [problem] = pinned.verify()
    assert "ask the owner" in problem and pinned.PINNED.classifier.sha256 in problem


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


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=pinned.REPO_ROOT, capture_output=True, text=True, check=False
    )


def test_shipped_files_are_not_git_ignored() -> None:
    """A teammate gets these from git: none of them may be covered by .gitignore."""
    shipped = [
        pinned.PINNED.cases.path,
        pinned.PINNED.manifest,
        pinned.PINNED.metrics,
        pinned.PINNED.sums,
        pinned.PINNED_DIR / "NOTICE.md",
    ]
    result = _git("check-ignore", *map(str, shipped))
    assert result.returncode == 1, f"git-ignored: {result.stdout}"


def test_the_classifier_is_git_ignored_so_that_it_is_never_committed() -> None:
    """ADR-0012 decision 15: the owner's copy sits in pinned/ but must never reach a commit."""
    result = _git("check-ignore", str(pinned.PINNED.classifier.path))
    assert result.returncode == 0, "add the classifier's exact path to .gitignore"


def test_the_classifier_is_not_tracked_by_git() -> None:
    result = _git("ls-files", "--", str(pinned.PINNED.classifier.path))
    assert result.returncode == 0 and result.stdout == ""


def test_the_notice_says_why_the_classifier_is_not_in_git() -> None:
    notice = (pinned.REPO_ROOT / pinned.PINNED_DIR / "NOTICE.md").read_text(encoding="utf-8")
    assert "xTRam1/safe-guard-prompt-injection" in notice
    assert "not redistributed" in notice and "not in git" in notice
    assert pinned.PINNED.classifier.sha256 in notice
    assert "ADR-0012" in notice


_MIT_PERMISSION_START = "Permission is hereby granted, free of charge, to any person"
_MIT_END = "SOFTWARE OR THE USE OR OTHER DEALINGS IN THE"


def test_the_notice_carries_the_licenses_of_the_two_sources_of_the_committed_cases() -> None:
    """MIT asks that the copyright and permission notice travel with the data (verbatim)."""
    notice = (pinned.REPO_ROOT / pinned.PINNED_DIR / "NOTICE.md").read_text(encoding="utf-8")
    assert "Copyright (c) Microsoft Corporation." in notice
    assert "Copyright (c) 2024 Runpeng Geng" in notice
    assert notice.count(_MIT_PERMISSION_START) >= 2
    assert notice.count("THE SOFTWARE IS PROVIDED") >= 2
    assert notice.count(_MIT_END) >= 2
    for url in (
        "https://github.com/microsoft/llmail-inject-challenge/blob/main/LICENSE",
        "https://github.com/sleeepeer/PoisonedRAG/blob/main/LICENSE",
    ):
        assert url in notice
