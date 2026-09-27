"""Unit tests for integration-test isolation helpers (RA.2)."""

import pytest

from packages.core.settings import BrokerSettings, DatabaseSettings
from tests.integration.isolation import (
    IsolationError,
    database_from_dsn,
    maintenance_dsn,
    validate_test_name,
    vhost_from_amqp_url,
)


@pytest.mark.parametrize("name", ["rag_email_test", "a_test", "x1_test", "retry_ab12_test"])
def test_validate_test_name_accepts_safe_names(name: str) -> None:
    assert validate_test_name(name) == name


@pytest.mark.parametrize("name", ["rag_email", "/", "", "rag-email_test", "Rag_test", "a/b_test"])
def test_validate_test_name_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(IsolationError):
        validate_test_name(name)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("amqp://guest:guest@localhost:5672/", "/"),
        ("amqp://guest:guest@localhost:5672", "/"),
        ("amqp://guest:guest@localhost:5672/%2F", "/"),
        ("amqp://guest:guest@localhost:5672/rag_email_test", "rag_email_test"),
    ],
)
def test_vhost_from_amqp_url_matches_aiormq(url: str, expected: str) -> None:
    assert vhost_from_amqp_url(url) == expected


def test_settings_url_round_trips_test_vhost() -> None:
    assert vhost_from_amqp_url(BrokerSettings(vhost="rag_email_test").url) == "rag_email_test"
    assert vhost_from_amqp_url(BrokerSettings(vhost="/").url) == "/"


def test_database_from_dsn_and_maintenance_dsn() -> None:
    db = DatabaseSettings(name="rag_email_test", port=5433)
    assert database_from_dsn(db.asyncpg_dsn) == "rag_email_test"
    assert database_from_dsn(maintenance_dsn(db)) == "postgres"
    assert db.name == "rag_email_test"  # model_copy did not mutate the original
