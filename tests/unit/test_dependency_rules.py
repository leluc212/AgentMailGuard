"""Architectural boundary and dependency enforcement tests.

Enforces rules defined in specs/design.md §4 and GEMINI.md:
1. services/* may import packages/*, but packages/* must NEVER import services/*.
2. packages/domain imports NOTHING but standard library and packages/core.
3. Provider names ('gmail', 'graph', 'imap') appear ONLY inside packages/adapters/.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PACKAGES_DIR = REPO_ROOT / "packages"
SERVICES_DIR = REPO_ROOT / "services"

STDLIB_MODULES = set(sys.stdlib_module_names)


def get_imports(file_path: Path) -> list[str]:
    """Parse a python file and return all imported module names."""
    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(file_path))
    except Exception as err:
        pytest.fail(f"Failed to parse {file_path}: {err}")

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    return imported


def test_packages_never_import_services() -> None:
    """Verify packages/* never imports services/*."""
    violations: list[str] = []
    for py_file in PACKAGES_DIR.rglob("*.py"):
        for mod in get_imports(py_file):
            if mod == "services" or mod.startswith("services."):
                violations.append(f"{py_file.relative_to(REPO_ROOT)} imports '{mod}'")

    assert not violations, "Forbidden service imports in packages:\n" + "\n".join(violations)


def test_packages_domain_imports_stdlib_and_core_only() -> None:
    """Verify packages/domain imports only standard library and packages/core."""
    domain_dir = PACKAGES_DIR / "domain"
    if not domain_dir.exists():
        return

    violations: list[str] = []
    for py_file in domain_dir.rglob("*.py"):
        for mod in get_imports(py_file):
            top_level = mod.split(".")[0]
            if top_level in STDLIB_MODULES:
                continue
            if mod == "packages.core" or mod.startswith("packages.core."):
                continue
            if (
                mod == "packages.domain"
                or mod.startswith("packages.domain.")
                or mod == "domain"
                or mod.startswith("domain.")
            ):
                continue
            violations.append(
                f"{py_file.relative_to(REPO_ROOT)} imports '{mod}' (only stdlib/core allowed)"
            )

    assert not violations, "Domain package boundary violations:\n" + "\n".join(violations)


def test_provider_names_confined_to_adapters() -> None:
    """Verify provider names (gmail, graph, imap) appear only in packages/adapters."""
    forbidden_tokens = {"gmail", "graph", "imap"}
    violations: list[str] = []

    for scan_dir in [SERVICES_DIR, PACKAGES_DIR]:
        for py_file in scan_dir.rglob("*.py"):
            if "packages/adapters" in str(py_file):
                continue

            for mod in get_imports(py_file):
                parts = set(mod.lower().split("."))
                intersect = parts.intersection(forbidden_tokens)
                if intersect:
                    violations.append(
                        f"{py_file.relative_to(REPO_ROOT)} imports provider module '{mod}'"
                    )

    assert not violations, "Provider modules imported outside packages/adapters:\n" + "\n".join(
        violations
    )


def test_services_never_reference_provider_literals() -> None:
    """Verify services/* code never references provider string literals directly (R1.3)."""
    forbidden_literals = {"gmail", "graph", "imap"}
    violations: list[str] = []

    for py_file in SERVICES_DIR.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except Exception as err:
            pytest.fail(f"Failed to parse {py_file}: {err}")

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.lower() in forbidden_literals
            ):
                msg = f"{py_file.relative_to(REPO_ROOT)}:{node.lineno} references '{node.value}'"
                violations.append(msg)

    assert not violations, "Provider literals hardcoded in services:\n" + "\n".join(violations)
