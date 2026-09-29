"""Static scoping proof for PostgresBusinessDataProvider's fixed queries (R13.4, CLAUDE.md §4)."""

from __future__ import annotations

import re

import pytest

from packages.business import postgres
from packages.business.postgres import TENANT_SCOPED_QUERIES


def test_every_sql_constant_is_listed_as_tenant_scoped() -> None:
    constants = {
        name
        for name, value in vars(postgres).items()
        if name.endswith("_SQL") and name != "SET_STATEMENT_TIMEOUT_SQL" and isinstance(value, str)
    }
    listed = {name for name, value in vars(postgres).items() if value in TENANT_SCOPED_QUERIES}
    assert constants == listed
    assert len(TENANT_SCOPED_QUERIES) == 5


@pytest.mark.parametrize("sql", TENANT_SCOPED_QUERIES)
def test_query_filters_on_organization_first(sql: str) -> None:
    where = re.search(r"WHERE\s+(.*?)(ORDER BY|LIMIT|$)", sql, re.S)
    assert where is not None
    assert where.group(1).strip().startswith("organization_id = $1")


@pytest.mark.parametrize("sql", TENANT_SCOPED_QUERIES[1:])
def test_entity_queries_are_scoped_to_the_resolved_customer(sql: str) -> None:
    assert "customer_id = $2" in sql


def test_sender_match_is_case_insensitive_on_the_whole_address() -> None:
    assert "lower(email) = lower($2)" in postgres.CUSTOMERS_BY_EMAIL_SQL
    assert "LIKE" not in postgres.CUSTOMERS_BY_EMAIL_SQL.upper()
