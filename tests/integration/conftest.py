"""Integration-test isolation from the live docker compose stack (see isolation.py)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from packages.core.settings import AppSettings
from packages.db.migrator import apply_migrations
from tests.integration.isolation import (
    install_connection_guards,
    isolation_enabled,
    reset_database,
    reset_vhost,
    resolve_test_database,
    resolve_test_vhost,
)


@pytest.fixture(scope="package", autouse=True)
def isolated_infrastructure() -> Iterator[None]:
    """Point every integration test at a fresh, migrated test vhost and database."""
    if not isolation_enabled():
        yield
        return

    vhost = resolve_test_vhost()
    database = resolve_test_database()
    mp = pytest.MonkeyPatch()
    try:
        # Env vars beat .env in pydantic-settings; AppSettings() resolves lazily inside
        # fixtures and tests, so this redirects every settings instance created later.
        mp.setenv("BROKER__VHOST", vhost)
        mp.setenv("DATABASE__NAME", database)
        settings = AppSettings()

        reset_vhost(settings.broker, vhost)
        asyncio.run(reset_database(settings.database, database))
        asyncio.run(apply_migrations(dsn=settings.database.asyncpg_dsn))

        install_connection_guards(mp, vhost=vhost, database=database)
        yield
    finally:
        mp.undo()
