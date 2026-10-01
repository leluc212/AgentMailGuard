"""Fixtures for the review UI browser tests (task 6.8; R23.4, R24.5).

The database is isolated exactly as tests/integration does it (rag_email_test, reset and
migrated once for this package). Playwright's sync API is started inside a package fixture
that depends on that isolation, so no asyncio loop runs in the test thread while it is live.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from packages.core.settings import AppSettings
from packages.db.migrator import apply_migrations
from tests.e2e.review_stack import ReviewStack, delete_organization
from tests.integration.isolation import (
    install_connection_guards,
    isolation_enabled,
    reset_database,
    resolve_test_database,
    resolve_test_vhost,
)


@pytest.fixture(scope="package", autouse=True)
def isolated_database() -> Iterator[None]:
    """Point the browser tests at a fresh, migrated rag_email_test database."""
    if not isolation_enabled():
        yield
        return
    database, vhost = resolve_test_database(), resolve_test_vhost()
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("DATABASE__NAME", database)
        mp.setenv("BROKER__VHOST", vhost)
        settings = AppSettings()
        asyncio.run(reset_database(settings.database, database))
        asyncio.run(apply_migrations(dsn=settings.database.asyncpg_dsn))
        install_connection_guards(mp, vhost=vhost, database=database)
        yield
    finally:
        mp.undo()


@pytest.fixture(scope="package")
def browser(isolated_database: None) -> Iterator[Browser]:
    with sync_playwright() as playwright:
        launched = playwright.chromium.launch()
        try:
            yield launched
        finally:
            launched.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    opened = context.new_page()
    opened.set_default_timeout(10_000)
    try:
        yield opened
    finally:
        context.close()


@pytest.fixture
def stack(isolated_database: None) -> Iterator[ReviewStack]:
    review_stack = ReviewStack()
    review_stack.start()
    try:
        yield review_stack
    finally:
        try:
            review_stack.run(delete_organization(review_stack.db, review_stack.organization_id))
        finally:
            review_stack.stop()
