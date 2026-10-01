"""Isolate integration tests from the running docker compose stack (RA.2, R24.4).

Integration tests share the RabbitMQ and PostgreSQL *servers* with the compose workers.
They are redirected to a dedicated vhost and database so workers never consume test
messages and tests never roll back or purge live state. Connection guards turn any
attempt to reach a non-test vhost or database into IsolationError.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

import aio_pika
import asyncpg
import httpx
import pytest

from packages.core.settings import BrokerSettings, DatabaseSettings

OPT_OUT_ENV = "RAG_EMAIL_TEST_USE_CONFIGURED_ENV"
VHOST_ENV = "RAG_EMAIL_TEST_BROKER_VHOST"
DATABASE_ENV = "RAG_EMAIL_TEST_DATABASE_NAME"
MGMT_URL_ENV = "RAG_EMAIL_TEST_RABBITMQ_MGMT_URL"

DEFAULT_TEST_VHOST = "rag_email_test"
DEFAULT_TEST_DATABASE = "rag_email_test"
MAINTENANCE_DATABASE = "postgres"

_SAFE_TEST_NAME = re.compile(r"[a-z0-9_]+_test")

# Vhosts/databases the guards currently accept. scratch_vhost() adds and removes entries.
_allowed_vhosts: set[str] = set()
_allowed_databases: set[str] = set()


class IsolationError(RuntimeError):
    """Raised when an integration test reaches a non-test vhost or database."""


def isolation_enabled() -> bool:
    """Return False only when the caller explicitly opts out of isolation."""
    return os.environ.get(OPT_OUT_ENV, "") != "1"


def validate_test_name(name: str) -> str:
    """Accept only lowercase names ending in _test (safe to drop, URL-safe)."""
    if not _SAFE_TEST_NAME.fullmatch(name):
        raise IsolationError(
            f"Refusing to use {name!r}: test vhost/database names must match "
            f"{_SAFE_TEST_NAME.pattern!r}"
        )
    return name


def resolve_test_vhost() -> str:
    return validate_test_name(os.environ.get(VHOST_ENV, DEFAULT_TEST_VHOST))


def resolve_test_database() -> str:
    return validate_test_name(os.environ.get(DATABASE_ENV, DEFAULT_TEST_DATABASE))


def vhost_from_amqp_url(url: object) -> str:
    """Resolve the vhost exactly as aiormq does: empty path or '/' means '/'."""
    path = urlsplit(str(url)).path
    if path in ("", "/"):
        return "/"
    return unquote(path[1:])


def database_from_dsn(dsn: object) -> str:
    return unquote(urlsplit(str(dsn)).path.lstrip("/"))


def management_url(broker: BrokerSettings) -> str:
    return os.environ.get(MGMT_URL_ENV) or f"http://{broker.host}:15672"


def maintenance_dsn(database: DatabaseSettings) -> str:
    return database.model_copy(update={"name": MAINTENANCE_DATABASE}).asyncpg_dsn


def reset_vhost(broker: BrokerSettings, vhost: str) -> None:
    """Drop and recreate a test vhost, then grant the broker user full rights."""
    validate_test_name(vhost)
    name = quote(vhost, safe="")
    user = quote(broker.user, safe="")
    with httpx.Client(
        base_url=management_url(broker),
        auth=(broker.user, broker.password),
        timeout=10.0,
    ) as client:
        deleted = client.delete(f"/api/vhosts/{name}")
        if deleted.status_code not in (204, 404):
            deleted.raise_for_status()
        client.put(
            f"/api/vhosts/{name}", json={"description": "rag-email integration tests"}
        ).raise_for_status()
        client.put(
            f"/api/permissions/{name}/{user}",
            json={"configure": ".*", "write": ".*", "read": ".*"},
        ).raise_for_status()


def delete_vhost(broker: BrokerSettings, vhost: str) -> None:
    """Delete a test vhost; a missing vhost is not an error."""
    validate_test_name(vhost)
    with httpx.Client(
        base_url=management_url(broker),
        auth=(broker.user, broker.password),
        timeout=10.0,
    ) as client:
        deleted = client.delete(f"/api/vhosts/{quote(vhost, safe='')}")
        if deleted.status_code not in (204, 404):
            deleted.raise_for_status()


async def reset_database(database: DatabaseSettings, name: str) -> None:
    """Drop and recreate the test database via the maintenance database."""
    validate_test_name(name)
    conn = await asyncpg.connect(maintenance_dsn(database))
    try:
        # CREATE/DROP DATABASE cannot run inside a transaction block; asyncpg
        # autocommits statements executed outside conn.transaction().
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


def install_connection_guards(mp: pytest.MonkeyPatch, *, vhost: str, database: str) -> None:
    """Make any connection to a non-test vhost or database fail loudly."""
    _allowed_vhosts.clear()
    _allowed_vhosts.add(vhost)
    _allowed_databases.clear()
    _allowed_databases.add(database)

    real_connect_robust: Callable[..., Coroutine[Any, Any, Any]] = aio_pika.connect_robust
    real_connect: Callable[..., Coroutine[Any, Any, Any]] = asyncpg.connect
    real_create_pool: Callable[..., Any] = asyncpg.create_pool

    def check_vhost(url: object, kwargs: dict[str, Any]) -> None:
        actual = vhost_from_amqp_url(url) if url is not None else kwargs.get("virtualhost", "/")
        if actual not in _allowed_vhosts:
            raise IsolationError(
                f"Integration test tried to use RabbitMQ vhost {actual!r}; allowed: "
                f"{sorted(_allowed_vhosts)}. Build broker settings with AppSettings().broker."
            )

    def check_database(dsn: object, kwargs: dict[str, Any]) -> None:
        actual = database_from_dsn(dsn) if dsn is not None else kwargs.get("database")
        if actual not in _allowed_databases:
            raise IsolationError(
                f"Integration test tried to use database {actual!r}; allowed: "
                f"{sorted(_allowed_databases)}. Build settings with AppSettings().database."
            )

    async def guarded_connect_robust(url: Any = None, *args: Any, **kwargs: Any) -> Any:
        check_vhost(url, kwargs)
        return await real_connect_robust(url, *args, **kwargs)

    async def guarded_connect(dsn: Any = None, *args: Any, **kwargs: Any) -> Any:
        check_database(dsn, kwargs)
        return await real_connect(dsn, *args, **kwargs)

    def guarded_create_pool(dsn: Any = None, *args: Any, **kwargs: Any) -> Any:
        check_database(dsn, kwargs)
        return real_create_pool(dsn, *args, **kwargs)

    mp.setattr(aio_pika, "connect_robust", guarded_connect_robust)
    mp.setattr(asyncpg, "connect", guarded_connect)
    mp.setattr(asyncpg, "create_pool", guarded_create_pool)


@asynccontextmanager
async def scratch_vhost(broker: BrokerSettings, prefix: str) -> AsyncIterator[BrokerSettings]:
    """Create a throwaway vhost for tests that need their own topology arguments.

    Yields a copy of ``broker`` pointing at the new vhost. The guards accept it only
    while the context is open; the vhost is deleted on exit.
    """
    name = validate_test_name(f"{prefix}_{uuid4().hex[:10]}_test")
    await asyncio.to_thread(reset_vhost, broker, name)
    _allowed_vhosts.add(name)
    try:
        yield broker.model_copy(update={"vhost": name})
    finally:
        _allowed_vhosts.discard(name)
        await asyncio.to_thread(delete_vhost, broker, name)
