from typing import Any

from packages.db.checkpoint import (
    CheckpointStore,
    InMemoryCheckpointStore,
    PostgresCheckpointStore,
)
from packages.db.connection import create_db_pool, create_pool_from_settings
from packages.db.idempotency import PostgresIdempotencyBackend
from packages.db.mailbox import (
    InMemoryMailboxStore,
    MailboxStore,
    PostgresMailboxStore,
)
from packages.db.message import (
    AttachmentRecord,
    InMemoryMessageStore,
    MessageInsertResult,
    MessageStore,
    PostgresMessageStore,
)
from packages.db.migrator import (
    Migration,
    apply_migrations,
    discover_migrations,
    get_applied_migrations,
    get_migration_status,
    rollback_migrations,
    verify_database_vector_dimension,
)
from packages.db.subscription import (
    InMemorySubscriptionStore,
    PostgresSubscriptionStore,
    SubscriptionStore,
)
from packages.db.thread import (
    InMemoryThreadStore,
    PostgresThreadStore,
    ThreadStore,
)

__all__ = [
    "AttachmentRecord",
    "CheckpointStore",
    "InMemoryCheckpointStore",
    "InMemoryMailboxStore",
    "InMemoryMessageStore",
    "InMemorySubscriptionStore",
    "InMemoryThreadStore",
    "MailboxStore",
    "MessageInsertResult",
    "MessageStore",
    "Migration",
    "PostgresCheckpointStore",
    "PostgresIdempotencyBackend",
    "PostgresMailboxStore",
    "PostgresMessageStore",
    "PostgresSubscriptionStore",
    "PostgresThreadStore",
    "SeedSummary",
    "SubscriptionStore",
    "ThreadStore",
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
