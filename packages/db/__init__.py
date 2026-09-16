from typing import Any

from packages.db.connection import create_db_pool, create_pool_from_settings
from packages.db.idempotency import PostgresIdempotencyBackend
from packages.db.migrator import (
    Migration,
    apply_migrations,
    discover_migrations,
    get_applied_migrations,
    get_migration_status,
    rollback_migrations,
    verify_database_vector_dimension,
)

__all__ = [
    "Migration",
    "PostgresIdempotencyBackend",
    "SeedSummary",
    "apply_migrations",
    "create_db_pool",
    "create_pool_from_settings",
    "deterministic_embed",
    "discover_migrations",
    "get_applied_migrations",
    "get_migration_status",
    "rollback_migrations",
    "seed_database",
    "verify_database_vector_dimension",
]


def __getattr__(name: str) -> Any:
    if name in ("seed_database", "SeedSummary", "deterministic_embed"):
        from packages.db.seed import (
            SeedSummary,
            deterministic_embed,
            seed_database,
        )

        mapping = {
            "seed_database": seed_database,
            "SeedSummary": SeedSummary,
            "deterministic_embed": deterministic_embed,
        }
        return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
