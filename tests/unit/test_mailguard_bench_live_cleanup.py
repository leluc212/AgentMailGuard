"""Live runner cleanup: MinIO objects first, then the throwaway organization (task 7.20).

No network and no database: the object store and the pool are doubles that keep an ordered
log, so the tests can assert that objects are purged before the organization row goes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest
import urllib3
from minio.datatypes import Object
from minio.deleteobjects import DeleteError
from minio.error import S3Error

from evaluation.mailguard_bench.case_adapter import EVAL_ORG_PREFIX
from evaluation.mailguard_bench.live.cleanup import (
    CleanupOutcome,
    MinioObjectAdmin,
    discard_organization,
    live_organization,
    organization_buckets,
    purge_organization_objects,
    purge_stale_live_orgs,
)
from packages.core.settings import ObjectStorageSettings

BUCKETS = ("raw-mime", "attachments", "html", "knowledge-docs")


class FakeStore:
    """An in-memory object store with the semantics the purge relies on."""

    def __init__(self, log: list[str] | None = None) -> None:
        self.buckets: dict[str, set[str]] = {}
        self.log = log if log is not None else []
        self.list_fails: set[str] = set()
        self.refuse: set[str] = set()

    def put(self, bucket: str, *keys: str) -> None:
        self.buckets.setdefault(bucket, set()).update(keys)

    async def top_level_prefixes(self, bucket: str) -> list[str]:
        if bucket in self.list_fails:
            raise RuntimeError(f"{bucket} unreachable")
        return sorted(
            {f"{key.split('/', 1)[0]}/" for key in self.buckets.get(bucket, ()) if "/" in key}
        )

    async def list_keys(self, bucket: str, prefix: str) -> list[str]:
        return sorted(key for key in self.buckets.get(bucket, ()) if key.startswith(prefix))

    async def remove(self, bucket: str, keys: Sequence[str]) -> list[str]:
        failed: list[str] = []
        for key in keys:
            if key in self.refuse:
                failed.append(key)
            else:
                self.buckets[bucket].discard(key)
        self.log.append(f"purge {bucket} {len(keys) - len(failed)}")
        return failed


class FakePool:
    """Records every statement in the shared log; rows for ``fetch`` are scripted."""

    def __init__(self, log: list[str] | None = None) -> None:
        self.log = log if log is not None else []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.fetched: list[tuple[str, tuple[Any, ...]]] = []
        self.rows: list[dict[str, Any]] = []
        self.fail_delete = False

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        self.log.append(query.split()[0].lower())
        if self.fail_delete and query.startswith("DELETE"):
            raise ConnectionError("database went away")
        return "DELETE 1" if query.startswith("DELETE") else "INSERT 0 1"

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetched.append((query, args))
        return self.rows


def _seed(store: FakeStore, org: UUID, other: UUID) -> None:
    """One organization's objects in every bucket, next to another organization's."""
    store.put("raw-mime", f"raw/{org}/mbx/m1.eml", f"raw/{other}/mbx/m9.eml")
    store.put("attachments", f"attachments/{org}/m1/a1/report.pdf")
    store.put("html", f"html/{org}/mbx/m1.html", f"html/{other}/mbx/m9.html")
    store.put(
        "knowledge-docs",
        f"knowledge/{org}/d1/v1/article.md",
        f"knowledge/{org}/d2/v1/article.md",
        f"knowledge/{other}/d7/v1/article.md",
    )


def test_the_buckets_follow_the_settings_and_html_has_a_default_until_it_is_a_setting() -> None:
    plain = ObjectStorageSettings(bucket_raw_mime="r", bucket_attachments="a", bucket_knowledge="k")
    assert organization_buckets(plain) == ("r", "a", "html", "k")

    class WithHtml(
        ObjectStorageSettings
    ):  # what settings.py gains with OBJECT_STORAGE__BUCKET_HTML
        bucket_html: str = "h"

    assert organization_buckets(WithHtml())[2] == "h"


async def test_purge_deletes_the_organizations_keys_in_every_bucket_and_no_others() -> None:
    org, other = uuid4(), uuid4()
    store = FakeStore()
    _seed(store, org, other)

    report = await purge_organization_objects(store, org, BUCKETS)

    assert report.deleted == 5 and report.errors == ()
    assert store.buckets["raw-mime"] == {f"raw/{other}/mbx/m9.eml"}
    assert store.buckets["attachments"] == set()
    assert store.buckets["html"] == {f"html/{other}/mbx/m9.html"}
    assert store.buckets["knowledge-docs"] == {f"knowledge/{other}/d7/v1/article.md"}


async def test_the_first_path_segment_is_not_assumed() -> None:
    """Keys are ``<first>/<organization_id>/...``; the purge finds every ``<first>``."""
    org = uuid4()
    store = FakeStore()
    store.put("knowledge-docs", f"knowledge/{org}/d/v1/a.md", f"legacy/{org}/d/a.md", "loose.txt")

    report = await purge_organization_objects(store, org, ["knowledge-docs"])

    assert report.deleted == 2
    assert store.buckets["knowledge-docs"] == {"loose.txt"}


async def test_a_bucket_that_does_not_exist_yet_is_not_an_error() -> None:
    org = uuid4()
    store = FakeStore()
    store.put("raw-mime", f"raw/{org}/mbx/m1.eml")  # the html bucket was never created

    report = await purge_organization_objects(store, org, BUCKETS)

    assert report.deleted == 1 and report.errors == ()


async def test_one_failing_bucket_is_reported_and_the_others_are_still_purged() -> None:
    org, other = uuid4(), uuid4()
    store = FakeStore()
    _seed(store, org, other)
    store.list_fails.add("attachments")

    report = await purge_organization_objects(store, org, BUCKETS)

    assert report.deleted == 4
    assert len(report.errors) == 1 and "attachments" in report.errors[0]
    assert "RuntimeError" in report.errors[0]
    assert store.buckets["attachments"] == {f"attachments/{org}/m1/a1/report.pdf"}


async def test_keys_the_store_refuses_to_delete_are_reported() -> None:
    org = uuid4()
    store = FakeStore()
    store.put("raw-mime", f"raw/{org}/mbx/m1.eml", f"raw/{org}/mbx/m2.eml")
    store.refuse.add(f"raw/{org}/mbx/m2.eml")

    report = await purge_organization_objects(store, org, ["raw-mime"])

    assert report.deleted == 1
    assert len(report.errors) == 1 and f"raw/{org}/mbx/m2.eml" in report.errors[0]


async def test_the_organization_row_goes_only_after_its_objects() -> None:
    log: list[str] = []
    org, other = uuid4(), uuid4()
    store, pool = FakeStore(log), FakePool(log)
    _seed(store, org, other)

    outcome = await discard_organization(pool, store, org, buckets=BUCKETS)

    assert outcome == CleanupOutcome(org, 5, (), organization_deleted=True)
    assert log.index("delete") > max(i for i, line in enumerate(log) if line.startswith("purge"))
    assert pool.executed == [("DELETE FROM organization WHERE id = $1", (org,))]


async def test_a_failed_purge_keeps_the_organization_for_the_next_starts_retry() -> None:
    org = uuid4()
    store, pool = FakeStore(), FakePool()
    store.put("raw-mime", f"raw/{org}/mbx/m1.eml")
    store.refuse.add(f"raw/{org}/mbx/m1.eml")

    outcome = await discard_organization(pool, store, org, buckets=["raw-mime"])

    assert outcome.organization_deleted is False and outcome.errors
    assert pool.executed == []  # no DELETE: the stale purge still finds the org by its name


async def test_a_failing_row_delete_is_reported_not_raised() -> None:
    org = uuid4()
    store, pool = FakeStore(), FakePool()
    pool.fail_delete = True

    outcome = await discard_organization(pool, store, org, buckets=BUCKETS)

    assert outcome.organization_deleted is False
    assert "ConnectionError" in outcome.errors[0]


async def test_the_organization_is_named_so_the_stale_purge_finds_it() -> None:
    store, pool = FakeStore(), FakePool()
    label = "2026-09-29-qwen-live/C3 attack-llmail-0123456789ab"

    async with live_organization(pool, store, label=label, buckets=BUCKETS) as org:
        assert isinstance(org, UUID)

    (insert, delete) = pool.executed
    assert insert[0].startswith("INSERT INTO organization")
    assert insert[1] == (org, f"{EVAL_ORG_PREFIX} {label}")
    assert delete == ("DELETE FROM organization WHERE id = $1", (org,))


async def test_a_long_label_is_cut_to_the_name_column_like_v1() -> None:
    store, pool = FakeStore(), FakePool()
    async with live_organization(pool, store, label="x" * 400, buckets=[]):
        pass
    assert len(pool.executed[0][1][1]) == 200


async def test_cleanup_runs_when_the_case_raises_and_the_case_error_survives() -> None:
    org_seen: list[UUID] = []
    store, pool = FakeStore(), FakePool()
    pool.fail_delete = True  # cleanup fails too: it must not replace the case's own error

    with pytest.raises(RuntimeError, match="case boom"):
        async with live_organization(pool, store, label="r/C0 a", buckets=BUCKETS) as org:
            org_seen.append(org)
            raise RuntimeError("case boom")

    assert [q for q, _ in pool.executed if q.startswith("DELETE")] == [
        "DELETE FROM organization WHERE id = $1"
    ]


async def test_each_outcome_is_handed_to_the_caller_for_the_run_summary() -> None:
    seen: list[CleanupOutcome] = []
    store, pool = FakeStore(), FakePool()

    async with live_organization(
        pool, store, label="r/C0 a", buckets=BUCKETS, on_cleanup=seen.append
    ) as org:
        store.put("raw-mime", f"raw/{org}/mbx/m1.eml")

    assert len(seen) == 1 and seen[0].organization_id == org
    assert seen[0].objects_deleted == 1 and seen[0].organization_deleted


async def test_the_stale_purge_finds_this_scope_and_purges_objects_before_rows() -> None:
    log: list[str] = []
    a, b, other = uuid4(), uuid4(), uuid4()
    store, pool = FakeStore(log), FakePool(log)
    pool.rows = [{"id": a}, {"id": b}]
    _seed(store, a, other)
    store.put("knowledge-docs", f"knowledge/{b}/d9/v1/article.md")

    purged = await purge_stale_live_orgs(pool, store, scope="run_1/C0", buckets=BUCKETS)

    # starts_with, not LIKE: a run name such as ``full_run`` needs no wildcard escaping.
    assert pool.fetched == [
        (
            "SELECT id FROM organization WHERE starts_with(name, $1)",
            (f"{EVAL_ORG_PREFIX} run_1/C0 ",),
        )
    ]
    assert purged.organizations == 2 and purged.objects == 6 and purged.errors == ()
    assert [args for q, args in pool.executed if q.startswith("DELETE")] == [(a,), (b,)]
    assert store.buckets["raw-mime"] == {f"raw/{other}/mbx/m9.eml"}
    # each organization: its objects go, then its row, before the next organization starts
    assert log == [
        "purge raw-mime 1",
        "purge attachments 1",
        "purge html 1",
        "purge knowledge-docs 2",
        "delete",
        "purge knowledge-docs 1",
        "delete",
    ]


async def test_the_stale_purge_counts_only_the_organizations_it_really_deleted() -> None:
    a = uuid4()
    store, pool = FakeStore(), FakePool()
    pool.rows = [{"id": a}]
    store.put("raw-mime", f"raw/{a}/mbx/m1.eml")
    store.refuse.add(f"raw/{a}/mbx/m1.eml")

    purged = await purge_stale_live_orgs(pool, store, scope="r/C0", buckets=["raw-mime"])

    assert purged.organizations == 0 and purged.errors


# --- the MinIO adapter -------------------------------------------------------------------


class FakeMinio:
    """The three minio-py calls the adapter makes, with minio-py's own return types."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.objects: list[Object] = []
        self.delete_errors: list[DeleteError] = []
        self.missing_bucket = False

    def list_objects(
        self, bucket_name: str, prefix: str | None = None, recursive: bool = False
    ) -> Any:
        self.calls.append(("list", bucket_name, prefix, recursive))
        if self.missing_bucket:
            raise S3Error(
                urllib3.response.HTTPResponse(),
                "NoSuchBucket",
                "The specified bucket does not exist",
                None,
                None,
                None,
            )
        yield from self.objects

    def remove_objects(self, bucket_name: str, delete_object_list: Any) -> Any:
        names = [item.name for item in delete_object_list]
        self.calls.append(("remove", bucket_name, names))
        return iter(self.delete_errors)


def _admin(client: FakeMinio) -> MinioObjectAdmin:
    admin = MinioObjectAdmin(ObjectStorageSettings(endpoint="http://minio:9000"))
    admin._client = client  # type: ignore[assignment]  # noqa: SLF001
    return admin


def test_the_admin_reuses_the_storage_clients_endpoint_handling() -> None:
    admin = MinioObjectAdmin(ObjectStorageSettings(endpoint="https://minio.example:9000"))
    assert admin.endpoint == "minio.example:9000"


async def test_top_level_prefixes_are_the_directory_entries_only() -> None:
    client = FakeMinio()
    client.objects = [Object("b", "raw/", None), Object("b", "loose.txt", None, size=1)]

    assert await _admin(client).top_level_prefixes("b") == ["raw/"]
    assert client.calls == [("list", "b", None, False)]


async def test_list_keys_is_recursive_under_the_prefix_and_skips_directory_markers() -> None:
    client = FakeMinio()
    client.objects = [Object("b", "raw/o/", None), Object("b", "raw/o/m/x.eml", None, size=3)]

    assert await _admin(client).list_keys("b", "raw/o/") == ["raw/o/m/x.eml"]
    assert client.calls == [("list", "b", "raw/o/", True)]


async def test_a_missing_bucket_lists_as_empty() -> None:
    client = FakeMinio()
    client.missing_bucket = True
    admin = _admin(client)

    assert await admin.top_level_prefixes("html") == []
    assert await admin.list_keys("html", "html/o/") == []


async def test_remove_deletes_the_keys_and_returns_what_the_store_refused() -> None:
    client = FakeMinio()
    client.delete_errors = [DeleteError("AccessDenied", "denied", "raw/o/m/x.eml", None)]

    failed = await _admin(client).remove("b", ["raw/o/m/x.eml", "raw/o/m/y.eml"])

    assert client.calls == [("remove", "b", ["raw/o/m/x.eml", "raw/o/m/y.eml"])]
    assert len(failed) == 1 and "raw/o/m/x.eml" in failed[0] and "AccessDenied" in failed[0]
