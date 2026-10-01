"""Database connection and connection pool management."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import asyncpg
import pgvector.asyncpg

if TYPE_CHECKING:
    from packages.core.settings import DatabaseSettings

logger = logging.getLogger(__name__)


async def _init_connection(conn: asyncpg.Connection[Any]) -> None:
    """Initialize connection with pgvector codec registration."""
    await pgvector.asyncpg.register_vector(conn)


async def create_db_pool(
    dsn: str,
    min_size: int = 2,
    max_size: int = 10,
    **kwargs: Any,
) -> asyncpg.Pool[Any]:
    """Create an asyncpg connection pool with pgvector registration.

    Args:
        dsn: PostgreSQL connection string.
        min_size: Minimum number of connections in the pool.
        max_size: Maximum number of connections in the pool.
        **kwargs: Additional arguments forwarded to asyncpg.create_pool.

    Returns:
        Configured asyncpg.Pool.
    """
    logger.info("Initializing asyncpg connection pool (min=%d, max=%d)", min_size, max_size)
    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        init=_init_connection,
        **kwargs,
    )
    if pool is None:
        raise RuntimeError("Failed to create asyncpg connection pool.")
    return pool


async def create_pool_from_settings(settings: DatabaseSettings) -> asyncpg.Pool[Any]:
    """Create a connection pool using a DatabaseSettings configuration instance."""
    return await create_db_pool(
        dsn=settings.asyncpg_dsn,
        min_size=settings.pool_min,
        max_size=settings.pool_max,
    )
