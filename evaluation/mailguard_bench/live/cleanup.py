"""Throwaway organizations of the live runner and their MinIO objects (task 7.20; R5.8).

    live_organization: INSERT organization ─▶ the case runs ─▶ finally:
        purge_organization_objects   raw-mime · attachments · html · knowledge-docs
                                     (keys read ``<first>/<organization_id>/...``: list, delete)
        DELETE FROM organization     cascades to every row the organization owns

The v1 runner deleted only the organization row, because its in-process host wrote nothing to
object storage. The services do: the hand-off archives the raw MIME, the email-worker offloads
HTML bodies and attachments, the API stores each knowledge document. Deleting the row alone
would leave those objects behind for good, since nothing else knows the organization's id.
So the objects go first, and the row goes only when they are gone. If the purge fails the row
stays, and the next start's stale purge (``purge_stale_live_orgs``) finds the organization
by its name and tries again.

Cleanup never raises: it runs in a ``finally`` and must not replace the error of the case
that is being cleaned up. Its failures come back as a ``CleanupOutcome`` for the run summary.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4

from minio.deleteobjects import DeleteObject
from minio.error import S3Error

from evaluation.mailguard_bench.case_adapter import EVAL_ORG_PREFIX
from packages.core.settings import ObjectStorageSettings
from packages.core.storage import MinioObjectStorageClient

logger = logging.getLogger(__name__)

DEFAULT_HTML_BUCKET = "html"
"""Bucket of the offloaded HTML bodies until ``OBJECT_STORAGE__BUCKET_HTML`` (task 7.20,
package A) names it in the settings."""


class ObjectStoreAdmin(Protocol):
    """The listing and bulk delete a purge needs; ``StorageProtocol`` offers neither.

    A bucket that does not exist has nothing to purge: it lists as empty.
    """

    async def top_level_prefixes(self, bucket: str) -> list[str]:
        """The first path segments in ``bucket``, each with its trailing slash."""
        ...

    async def list_keys(self, bucket: str, prefix: str) -> list[str]:
        """Every object key under ``prefix``, at any depth."""
        ...

    async def remove(self, bucket: str, keys: Sequence[str]) -> list[str]:
        """Delete ``keys``; return one description per key the store refused to delete."""
        ...


class OrganizationPool(Protocol):
    """The two asyncpg pool methods the organization lifecycle uses."""

    async def execute(self, query: str, *args: Any) -> str: ...

    async def fetch(self, query: str, *args: Any) -> list[Any]: ...


@dataclass(frozen=True)
class PurgeReport:
    """What one organization's object purge deleted, and what it could not."""

    deleted: int
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class CleanupOutcome:
    """The result of discarding one organization: objects first, then its row."""

    organization_id: UUID
    objects_deleted: int
    errors: tuple[str, ...]
    organization_deleted: bool


@dataclass(frozen=True)
class StalePurge:
    """The result of the stale-organization purge at the start of a run."""

    organizations: int
    objects: int
    errors: tuple[str, ...]


def organization_buckets(settings: ObjectStorageSettings) -> tuple[str, ...]:
    """The buckets that can hold an organization's objects: raw MIME, attachments, HTML, docs."""
    return (
        settings.bucket_raw_mime,
        settings.bucket_attachments,
        getattr(settings, "bucket_html", DEFAULT_HTML_BUCKET),
        settings.bucket_knowledge,
    )


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


async def purge_organization_objects(
    admin: ObjectStoreAdmin, organization_id: UUID, buckets: Sequence[str]
) -> PurgeReport:
    """Delete every object of ``organization_id`` in ``buckets``; never raises.

    Every key has the organization id as its second path segment
    (``packages/core/storage.py`` ObjectKeyBuilder), so the first segment is read from the
    bucket instead of being assumed, and ``<first>/<organization_id>/`` selects exactly one
    organization. A failing bucket is reported and the others are still purged.
    """
    deleted = 0
    errors: list[str] = []
    for bucket in buckets:
        try:
            keys: list[str] = []
            for first in await admin.top_level_prefixes(bucket):
                keys += await admin.list_keys(bucket, f"{first.rstrip('/')}/{organization_id}/")
            failed = await admin.remove(bucket, keys) if keys else []
        except Exception as exc:
            errors.append(f"{bucket}: {_error_text(exc)}")
            continue
        deleted += len(keys) - len(failed)
        errors.extend(f"{bucket}: could not delete {detail}" for detail in failed)
    return PurgeReport(deleted=deleted, errors=tuple(errors))


async def discard_organization(
    pool: OrganizationPool,
    admin: ObjectStoreAdmin,
    organization_id: UUID,
    *,
    buckets: Sequence[str],
) -> CleanupOutcome:
    """Purge the organization's objects, then delete its row (which cascades); never raises.

    The row stays when the purge failed, so the next start's stale purge can retry: once the
    row is gone nothing could find the leftover objects again.
    """
    report = await purge_organization_objects(admin, organization_id, buckets)
    if report.errors:
        logger.error(
            "organization %s keeps its row: its objects were not all deleted (%s)",
            organization_id,
            "; ".join(report.errors),
        )
        return CleanupOutcome(organization_id, report.deleted, report.errors, False)
    try:
        await pool.execute("DELETE FROM organization WHERE id = $1", organization_id)
    except Exception as exc:
        logger.error("organization %s was not deleted: %s", organization_id, _error_text(exc))
        return CleanupOutcome(
            organization_id, report.deleted, (f"organization delete: {_error_text(exc)}",), False
        )
    return CleanupOutcome(organization_id, report.deleted, (), True)


@asynccontextmanager
async def live_organization(
    pool: OrganizationPool,
    admin: ObjectStoreAdmin,
    *,
    label: str,
    buckets: Sequence[str],
    on_cleanup: Callable[[CleanupOutcome], None] | None = None,
) -> AsyncIterator[UUID]:
    """A throwaway organization, discarded with its MinIO objects on exit whatever happens.

    Named like v1's ``eval_organization`` (``mailguard-bench <label>``), so the stale purge
    finds it by prefix. ``on_cleanup`` receives the outcome for the run summary.
    """
    organization_id = uuid4()
    name = f"{EVAL_ORG_PREFIX} {label}"[:200]
    await pool.execute("INSERT INTO organization (id, name) VALUES ($1, $2)", organization_id, name)
    try:
        yield organization_id
    finally:
        outcome = await discard_organization(pool, admin, organization_id, buckets=buckets)
        if on_cleanup is not None:
            on_cleanup(outcome)


async def purge_stale_live_orgs(
    pool: OrganizationPool, admin: ObjectStoreAdmin, *, scope: str, buckets: Sequence[str]
) -> StalePurge:
    """Discard this RUN/CONFIG's organizations left behind by a killed run.

    Scoped on purpose, as v1's purge: another config may be running in a second terminal, and
    a global purge would delete the knowledge base of the case it has in flight. The runner
    holds a per-RUN/CONFIG advisory lock, so nothing else owns organizations under this scope
    while the purge runs. ``starts_with`` matches the prefix literally, so a run name such as
    ``full_run`` needs no LIKE escaping.
    """
    rows = await pool.fetch(
        "SELECT id FROM organization WHERE starts_with(name, $1)", f"{EVAL_ORG_PREFIX} {scope} "
    )
    outcomes = [await discard_organization(pool, admin, row["id"], buckets=buckets) for row in rows]
    return StalePurge(
        organizations=sum(outcome.organization_deleted for outcome in outcomes),
        objects=sum(outcome.objects_deleted for outcome in outcomes),
        errors=tuple(error for outcome in outcomes for error in outcome.errors),
    )


class MinioObjectAdmin(MinioObjectStorageClient):
    """The MinIO client's listing and bulk delete, on the storage client's own settings.

    minio-py is synchronous, so every call runs in a worker thread, as the storage client's
    calls do.
    """

    async def top_level_prefixes(self, bucket: str) -> list[str]:
        """The directory entries at the bucket root (``raw/``, ``html/``, ...)."""

        def run() -> list[str]:
            try:
                return [
                    obj.object_name
                    for obj in self._client.list_objects(bucket)
                    if obj.is_dir and obj.object_name
                ]
            except S3Error as err:
                if err.code == "NoSuchBucket":
                    return []
                raise

        return await asyncio.to_thread(run)

    async def list_keys(self, bucket: str, prefix: str) -> list[str]:
        """Object keys under ``prefix`` at any depth; directory markers are not objects."""

        def run() -> list[str]:
            try:
                return [
                    obj.object_name
                    for obj in self._client.list_objects(bucket, prefix=prefix, recursive=True)
                    if obj.object_name and not obj.is_dir
                ]
            except S3Error as err:
                if err.code == "NoSuchBucket":
                    return []
                raise

        return await asyncio.to_thread(run)

    async def remove(self, bucket: str, keys: Sequence[str]) -> list[str]:
        """Delete ``keys``; the errors are described, one per key that stayed."""

        def run() -> list[str]:
            # remove_objects is lazy: nothing is deleted until its error iterator is read.
            errors = self._client.remove_objects(bucket, [DeleteObject(key) for key in keys])
            return [str(error) for error in errors]

        return await asyncio.to_thread(run)
