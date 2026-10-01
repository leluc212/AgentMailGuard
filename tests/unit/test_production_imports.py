"""Production import-path guard (RA.1, R20.1).

Every module shipped in the runtime image (``packages/`` and ``services/``) must import
without dev-only dependencies (pytest and its plugins are never installed in the image)
and without repo-root trees the image does not contain (``evaluation/``, ``tests/``).

The probe runs in a fresh interpreter so the pytest already loaded in this process
cannot mask a missing import.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_ROOTS = ("packages", "services")
DEV_ONLY_ROOTS = (
    "pytest",
    "_pytest",
    "pytest_asyncio",
    "pytest_cov",
    "pytest_mock",
    "mypy",
    "playwright",
)
TEST_SUPPORT_MODULE = "testing"  # packages/*/testing.py: contract suites, test-only by design

ENTRYPOINTS = (
    "services.api.main",
    "services.frontend.main",
    "services.dispatch_worker.main",
    "services.email_worker.main",
    "services.knowledge_worker.main",
    "services.triage_worker.consumer",
    "services.mail_connector.orchestrator",
    "packages.context.builder",
)

_PROBE = textwrap.dedent(
    """
    import importlib
    import importlib.abc
    import json
    import sys
    import traceback

    blocked = frozenset(json.loads(sys.argv[1]))
    modules = json.loads(sys.argv[2])
    first_party = tuple(json.loads(sys.argv[3]))


    class _Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.partition(".")[0] in blocked:
                raise ModuleNotFoundError(
                    f"No module named {fullname!r} (not available in the production image)",
                    name=fullname,
                )
            return None


    sys.meta_path.insert(0, _Blocker())
    failures = {}
    for name in modules:
        try:
            importlib.import_module(name)
        except BaseException as exc:
            tb = traceback.extract_tb(exc.__traceback__)
            frame = next((f for f in reversed(tb) if f.filename != "<string>"), tb[-1])
            where = f"{frame.filename}:{frame.lineno}"
            failures[name] = f"{type(exc).__name__}: {exc} (raised at {where})"
            # A failed package __init__ leaves its already-imported submodules cached, which
            # would let later imports of the same chain succeed; start the next one clean.
            for key in [k for k in sys.modules if k.partition(".")[0] in first_party]:
                del sys.modules[key]
    print(json.dumps(failures))
    """
)


def _unshipped_repo_packages() -> list[str]:
    return sorted(
        p.name
        for p in REPO_ROOT.iterdir()
        if p.is_dir() and (p / "__init__.py").is_file() and p.name not in SHIPPED_ROOTS
    )


def _production_modules() -> list[str]:
    names: list[str] = []
    for root in SHIPPED_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            if parts[-1] == TEST_SUPPORT_MODULE:
                continue
            names.append(".".join(parts))
    return names


def _probe(modules: list[str], blocked: list[str], cwd: Path) -> dict[str, str]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}  # mirrors Dockerfile ENV PYTHONPATH=/app
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE,
            json.dumps(blocked),
            json.dumps(modules),
            json.dumps(list(SHIPPED_ROOTS)),
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result: dict[str, str] = json.loads(completed.stdout.strip().splitlines()[-1])
    return result


def test_probe_blocks_dev_only_and_unshipped_modules(tmp_path: Path) -> None:
    """The probe itself must be able to fail; otherwise the guard below is vacuous."""
    blocked = [*DEV_ONLY_ROOTS, *_unshipped_repo_packages()]
    failures = _probe(["pytest", "evaluation"], blocked, tmp_path)
    assert set(failures) == {"pytest", "evaluation"}


def test_entrypoints_are_covered_by_module_walk() -> None:
    """Renaming an entrypoint must not silently drop it from the import guard."""
    missing = sorted(set(ENTRYPOINTS) - set(_production_modules()))
    assert not missing, f"entrypoints not found under {SHIPPED_ROOTS}: {missing}"


def test_production_modules_import_without_dev_or_unshipped_dependencies(
    tmp_path: Path,
) -> None:
    blocked = [*DEV_ONLY_ROOTS, *_unshipped_repo_packages()]
    modules = [*ENTRYPOINTS, *(m for m in _production_modules() if m not in ENTRYPOINTS)]
    failures = _probe(modules, blocked, tmp_path)
    report = "\n".join(f"  {name}: {reason}" for name, reason in sorted(failures.items()))
    assert not failures, f"{len(failures)} production module(s) fail to import:\n{report}"
