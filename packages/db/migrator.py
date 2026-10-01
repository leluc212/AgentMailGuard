"""Versioned, reversible database migration engine and CLI.

Requirements:
- R5.1: PostgreSQL with pgvector authoritative storage.
- R5.5: Exclusive schema management via versioned, reversible migrations (no runtime DDL).
- R5.10: Fail fast at startup if configured embedding dimension != database column.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

from packages.core.settings import AppSettings, assert_embedding_dimension

logger = logging.getLogger(__name__)

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


@dataclass(frozen=True)
class Migration:
    """Represents a discovered SQL migration script pair."""

    version: str
    name: str
    up_path: Path
    down_path: Path
    checksum: str


def compute_checksum(content: str) -> str:
    """Calculate SHA256 checksum of SQL content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def discover_migrations(migrations_dir: Path | None = None) -> list[Migration]:
    """Discover and validate all versioned SQL migrations in ascending order."""
    target_dir = migrations_dir or DEFAULT_MIGRATIONS_DIR
    if not target_dir.exists():
        raise FileNotFoundError(f"Migrations directory does not exist: {target_dir}")

    up_files = sorted(target_dir.glob("*.up.sql"))
    migrations: list[Migration] = []

    for up_path in up_files:
        stem = up_path.name[: -len(".up.sql")]
        parts = stem.split("_", 1)
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1]:
            raise ValueError(
                f"Invalid migration filename format: {up_path.name}. "
                "Expected <version>_<name>.up.sql with numeric version"
            )

        version, name = parts
        down_path = target_dir / f"{version}_{name}.down.sql"
        if not down_path.exists():
            raise FileNotFoundError(
                f"Missing reversible down migration for {up_path.name}: Expected {down_path.name}"
            )

        content = up_path.read_text(encoding="utf-8")
        checksum = compute_checksum(content)
        migrations.append(
            Migration(
                version=version,
                name=name,
                up_path=up_path,
                down_path=down_path,
                checksum=checksum,
            )
        )

    return sorted(migrations, key=lambda m: m.version)


async def ensure_schema_migrations_table(conn: asyncpg.Connection[Any]) -> None:
    """Ensure the schema_migrations tracking table exists."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )


async def get_applied_migrations(conn: asyncpg.Connection[Any]) -> dict[str, dict[str, Any]]:
    """Retrieve all applied migrations ordered by version."""
    await ensure_schema_migrations_table(conn)
    rows = await conn.fetch(
        "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version ASC"
    )
    return {
        r["version"]: {
            "name": r["name"],
            "checksum": r["checksum"],
            "applied_at": r["applied_at"],
        }
        for r in rows
    }


async def apply_migrations(
    dsn: str | None = None,
    migrations_dir: Path | None = None,
) -> list[str]:
    """Apply all pending database migrations in sequential order."""
    resolved_dsn = dsn or AppSettings().database.asyncpg_dsn
    all_migrations = discover_migrations(migrations_dir)

    conn = await asyncpg.connect(resolved_dsn)
    try:
        await ensure_schema_migrations_table(conn)
        applied = await get_applied_migrations(conn)

        applied_versions: list[str] = []
        for migration in all_migrations:
            if migration.version in applied:
                record = applied[migration.version]
                if record["checksum"] != migration.checksum:
                    logger.warning(
                        "Migration %s checksum mismatch! Applied: %s, Current: %s",
                        migration.version,
                        record["checksum"],
                        migration.checksum,
                    )
                continue

            logger.info("Applying migration %s: %s", migration.version, migration.name)
            up_sql = migration.up_path.read_text(encoding="utf-8")

            async with conn.transaction():
                await conn.execute(up_sql)
                await conn.execute(
                    """
                    INSERT INTO schema_migrations (version, name, checksum, applied_at)
                    VALUES ($1, $2, $3, now());
                    """,
                    migration.version,
                    migration.name,
                    migration.checksum,
                )

            applied_versions.append(migration.version)
            logger.info("Successfully applied migration %s", migration.version)

        return applied_versions
    finally:
        await conn.close()


async def rollback_migrations(
    dsn: str | None = None,
    migrations_dir: Path | None = None,
    steps: int = 1,
) -> list[str]:
    """Roll back applied migrations in reverse order."""
    if steps < 1:
        raise ValueError("Rollback steps must be at least 1")

    resolved_dsn = dsn or AppSettings().database.asyncpg_dsn
    all_migrations_map = {m.version: m for m in discover_migrations(migrations_dir)}

    conn = await asyncpg.connect(resolved_dsn)
    try:
        await ensure_schema_migrations_table(conn)
        applied = await get_applied_migrations(conn)
        if not applied:
            logger.info("No migrations to roll back.")
            return []

        # Sort applied versions in descending order
        to_rollback = sorted(applied.keys(), reverse=True)[:steps]
        rolled_back: list[str] = []

        for version in to_rollback:
            if version not in all_migrations_map:
                raise RuntimeError(
                    f"Cannot rollback migration {version}: Migration definition file not found."
                )

            migration = all_migrations_map[version]
            logger.info("Rolling back migration %s: %s", migration.version, migration.name)
            down_sql = migration.down_path.read_text(encoding="utf-8")

            async with conn.transaction():
                await conn.execute(down_sql)
                await conn.execute(
                    "DELETE FROM schema_migrations WHERE version = $1;",
                    migration.version,
                )

            rolled_back.append(version)
            logger.info("Successfully rolled back migration %s", migration.version)

        return rolled_back
    finally:
        await conn.close()


async def get_migration_status(
    dsn: str | None = None,
    migrations_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return status report for all discovered migrations."""
    resolved_dsn = dsn or AppSettings().database.asyncpg_dsn
    all_migrations = discover_migrations(migrations_dir)

    conn = await asyncpg.connect(resolved_dsn)
    try:
        applied = await get_applied_migrations(conn)
        status_list: list[dict[str, Any]] = []

        for m in all_migrations:
            is_applied = m.version in applied
            applied_info = applied.get(m.version)
            status_list.append(
                {
                    "version": m.version,
                    "name": m.name,
                    "applied": is_applied,
                    "applied_at": applied_info["applied_at"] if applied_info else None,
                    "checksum": m.checksum,
                    "checksum_match": (
                        applied_info["checksum"] == m.checksum if applied_info else True
                    ),
                }
            )

        return status_list
    finally:
        await conn.close()


async def verify_database_vector_dimension(
    conn_or_dsn: asyncpg.Connection[Any] | str | None = None,
    configured_dimension: int | None = None,
) -> None:
    """Verify that database column VECTOR(n) matches configured embedding dimension (R5.10).

    Raises:
        RuntimeError: If dimension check fails or embedding_record table is missing.
    """
    settings = AppSettings()
    target_dim = configured_dimension or settings.embedding.dimension

    should_close = False
    if isinstance(conn_or_dsn, asyncpg.Connection):
        conn = conn_or_dsn
    else:
        resolved_dsn = conn_or_dsn or settings.database.asyncpg_dsn
        conn = await asyncpg.connect(resolved_dsn)
        should_close = True

    try:
        row = await conn.fetchrow(
            """
            SELECT a.atttypmod AS dim
            FROM pg_attribute a
            JOIN pg_class c ON a.attrelid = c.oid
            JOIN pg_namespace n ON c.relnamespace = n.oid
            WHERE n.nspname = 'public'
              AND c.relname = 'embedding_record'
              AND a.attname = 'embedding';
            """
        )
        if row is None or row["dim"] is None:
            raise RuntimeError(
                "Unable to determine vector column dimension: "
                "'embedding_record.embedding' not found."
            )

        actual_dim = int(row["dim"])
        # Use assertion from packages.core.settings (R5.10)
        assert_embedding_dimension(target_dim, actual_dim)
        logger.info(
            "Vector column dimension verified: configured=%d, database=%d", target_dim, actual_dim
        )
    finally:
        if should_close:
            await conn.close()


def main() -> None:
    """CLI entrypoint for database migrations."""
    parser = argparse.ArgumentParser(description="Database Migration Runner (R5.5)")
    parser.add_argument("command", choices=["up", "down", "status", "verify-dim"])
    parser.add_argument("--steps", type=int, default=1, help="Steps to rollback (for down)")
    parser.add_argument("--dsn", type=str, default=None, help="Database DSN override")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.command == "up":
        applied = asyncio.run(apply_migrations(dsn=args.dsn))
        if applied:
            print(f"Applied migrations: {', '.join(applied)}")
        else:
            print("Database is up to date. No pending migrations.")

    elif args.command == "down":
        rolled_back = asyncio.run(rollback_migrations(dsn=args.dsn, steps=args.steps))
        if rolled_back:
            print(f"Rolled back migrations: {', '.join(rolled_back)}")
        else:
            print("No migrations were rolled back.")

    elif args.command == "status":
        status_list = asyncio.run(get_migration_status(dsn=args.dsn))
        print(f"{'VERSION':<10} {'NAME':<25} {'APPLIED':<10} {'APPLIED AT'}")
        print("-" * 65)
        for s in status_list:
            applied_str = "YES" if s["applied"] else "NO"
            time_str = str(s["applied_at"]) if s["applied_at"] else "-"
            print(f"{s['version']:<10} {s['name']:<25} {applied_str:<10} {time_str}")

    elif args.command == "verify-dim":
        asyncio.run(verify_database_vector_dimension(conn_or_dsn=args.dsn))
        print("Embedding vector dimension matches database schema (R5.10 PASS).")


if __name__ == "__main__":
    main()
