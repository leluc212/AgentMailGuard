"""Prove integration tests are isolated from the compose stack's vhost and database (RA.2)."""

from urllib.parse import quote
from uuid import uuid4

import aio_pika
import asyncpg
import httpx
import pytest

from packages.core.settings import AppSettings
from packages.db.connection import create_pool_from_settings
from tests.integration.isolation import (
    IsolationError,
    isolation_enabled,
    management_url,
    resolve_test_database,
    resolve_test_vhost,
    scratch_vhost,
)

pytestmark = pytest.mark.skipif(
    not isolation_enabled(), reason="isolation disabled via RAG_EMAIL_TEST_USE_CONFIGURED_ENV=1"
)


def test_settings_resolve_to_test_vhost_and_database() -> None:
    settings = AppSettings()
    assert settings.broker.vhost == resolve_test_vhost()
    assert settings.database.name == resolve_test_database()
    assert settings.broker.url.endswith(f"/{resolve_test_vhost()}")


async def test_database_pool_lands_in_migrated_test_database() -> None:
    pool = await create_pool_from_settings(AppSettings().database)
    try:
        assert await pool.fetchval("SELECT current_database()") == resolve_test_database()
        applied = await pool.fetchval("SELECT count(*) FROM schema_migrations")
        assert applied >= 3
    finally:
        await pool.close()


async def test_queue_declared_by_tests_exists_only_in_test_vhost() -> None:
    broker = AppSettings().broker
    queue_name = f"isolation.probe.{uuid4().hex[:8]}"
    conn = await aio_pika.connect_robust(broker.url)
    try:
        channel = await conn.channel()
        # exclusive: the broker drops the probe with the connection, even if an assert fails
        await channel.declare_queue(queue_name, exclusive=True)
        async with httpx.AsyncClient(
            base_url=management_url(broker), auth=(broker.user, broker.password)
        ) as client:
            in_test = await client.get(
                f"/api/queues/{quote(resolve_test_vhost(), safe='')}/{queue_name}"
            )
            in_default = await client.get(f"/api/queues/%2F/{queue_name}")
        assert in_test.status_code == 200
        assert in_default.status_code == 404
    finally:
        await conn.close()


async def test_guard_rejects_default_vhost() -> None:
    with pytest.raises(IsolationError):
        await aio_pika.connect_robust("amqp://guest:guest@localhost:5672/")


async def test_guard_rejects_live_database() -> None:
    live = AppSettings().database.model_copy(update={"name": "rag_email"})
    with pytest.raises(IsolationError):
        await asyncpg.connect(live.asyncpg_dsn)
    with pytest.raises(IsolationError):
        await create_pool_from_settings(live)


async def test_scratch_vhost_is_usable_only_while_open() -> None:
    async with scratch_vhost(AppSettings().broker, "probe") as broker:
        assert broker.vhost.startswith("probe_") and broker.vhost.endswith("_test")
        conn = await aio_pika.connect_robust(broker.url)
        await conn.close()
    with pytest.raises(IsolationError):
        await aio_pika.connect_robust(broker.url)
