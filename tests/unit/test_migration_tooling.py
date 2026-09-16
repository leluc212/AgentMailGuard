"""Unit tests for database migration tooling and discovery (R5.5)."""

from pathlib import Path

import pytest

from packages.db.migrator import (
    Migration,
    compute_checksum,
    discover_migrations,
)


def test_compute_checksum() -> None:
    """Verify compute_checksum returns valid hex digest."""
    sql = "CREATE TABLE test (id INT);"
    c1 = compute_checksum(sql)
    c2 = compute_checksum(sql)
    assert len(c1) == 64
    assert c1 == c2


def test_discover_migrations_default() -> None:
    """Verify discovery finds existing core migration."""
    migrations = discover_migrations()
    assert len(migrations) >= 1
    m1 = migrations[0]
    assert isinstance(m1, Migration)
    assert m1.version == "0001"
    assert m1.name == "core_schema"
    assert m1.up_path.exists()
    assert m1.down_path.exists()
    assert len(m1.checksum) == 64


def test_discover_migrations_missing_down(tmp_path: Path) -> None:
    """Verify error raised when .down.sql is missing."""
    up_file = tmp_path / "0002_test.up.sql"
    up_file.write_text("CREATE TABLE test (id INT);", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="Missing reversible down migration"):
        discover_migrations(tmp_path)


def test_discover_migrations_invalid_filename(tmp_path: Path) -> None:
    """Verify error raised on invalid filename."""
    bad_file = tmp_path / "invalid_name.up.sql"
    bad_file.write_text("CREATE TABLE test (id INT);", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid migration filename format"):
        discover_migrations(tmp_path)
