"""Email message and attachment persistence store (R4.8, R5.4, R5.6, R4.7, R5.8).

Provides durable PostgreSQL and in-memory backends for:
- Idempotent email message insertion with ON CONFLICT DO NOTHING.
- Write-time generation of full-text search vector (search_tsv).
- Atomic persistence of attachment metadata linked to email_message.
- Full-text search and thread-based message retrieval with strict multi-tenant scoping.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

import asyncpg

from packages.domain.entities import AttachmentRef, EmailAddress, NormalizedMessage

logger = logging.getLogger(__name__)


def _to_uuid(val: UUID | str) -> UUID:
    return val if isinstance(val, UUID) else UUID(str(val))


def _clean_pg_str(val: str | None) -> str | None:
    if val is None:
        return None
    return val.replace("\x00", "")


@dataclass(frozen=True)
class MessageInsertResult:
    """Outcome of an email message insertion attempt (R4.8, R5.4)."""

    inserted: bool
    message_id: UUID
    thread_id: UUID
    is_duplicate: bool


@dataclass(frozen=True)
class AttachmentRecord:
    """Attachment metadata record matching attachment table schema (R4.7, R5.8)."""

    id: UUID
    organization_id: UUID
    message_id: UUID
    filename: str | None
    mime_type: str | None
    size_bytes: int | None
    object_key: str
    checksum: str | None = None
    created_at: datetime | None = None


@runtime_checkable
class MessageStore(Protocol):
    """Protocol defining message and attachment persistence operations."""

    async def insert_message(
        self,
        message: NormalizedMessage,
        attachments: Sequence[AttachmentRef] | None = None,
    ) -> MessageInsertResult:
        """Insert message and its attachments atomically with ON CONFLICT DO NOTHING."""
        ...

    async def get_message(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
    ) -> NormalizedMessage | None:
        """Retrieve a message by its primary ID within an organization."""
        ...

    async def get_message_by_provider_id(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        provider_message_id: str,
    ) -> NormalizedMessage | None:
        """Retrieve a message by provider-assigned ID within a mailbox."""
        ...

    async def get_attachments(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
    ) -> list[AttachmentRecord]:
        """Retrieve all attachment records for an email message."""
        ...

    async def get_messages_by_thread(
        self,
        organization_id: UUID | str,
        thread_id: UUID | str,
    ) -> list[NormalizedMessage]:
        """Retrieve all messages in a thread ordered by received_at ASC."""
        ...

    async def search_messages_by_text(
        self,
        organization_id: UUID | str,
        query: str,
        limit: int = 20,
    ) -> list[NormalizedMessage]:
        """Perform full-text search against message search_tsv."""
        ...


class InMemoryMessageStore:
    """In-memory message store for unit tests and isolated mocking."""

    def __init__(self) -> None:
        # Key: (organization_id, message_id) -> NormalizedMessage
        self.messages: dict[tuple[UUID, UUID], NormalizedMessage] = {}
        # Key: (organization_id, mailbox_id, provider_message_id) -> message_id
        self.unique_provider_map: dict[tuple[UUID, UUID, str], UUID] = {}
        # Key: (organization_id, message_id) -> list[AttachmentRecord]
        self.attachments: dict[tuple[UUID, UUID], list[AttachmentRecord]] = {}

    async def insert_message(
        self,
        message: NormalizedMessage,
        attachments: Sequence[AttachmentRef] | None = None,
    ) -> MessageInsertResult:
        org_u = _to_uuid(message.organization_id)
        mbx_u = _to_uuid(message.mailbox_id)
        msg_u = _to_uuid(message.message_id)
        thd_u = _to_uuid(message.thread_id)

        uniq_key = (org_u, mbx_u, message.provider_message_id)
        if uniq_key in self.unique_provider_map:
            logger.info(
                "Duplicate message rejected: org=%s, mailbox=%s, provider_msg_id=%s",
                org_u,
                mbx_u,
                message.provider_message_id,
            )
            return MessageInsertResult(
                inserted=False,
                message_id=self.unique_provider_map[uniq_key],
                thread_id=thd_u,
                is_duplicate=True,
            )

        # Merge attachments
        all_att_refs: list[AttachmentRef] = list(message.attachments)
        if attachments:
            for att in attachments:
                if att not in all_att_refs:
                    all_att_refs.append(att)

        # Persist message
        msg_copy = NormalizedMessage(
            message_id=msg_u,
            thread_id=thd_u,
            mailbox_id=mbx_u,
            organization_id=org_u,
            provider=message.provider,
            provider_message_id=message.provider_message_id,
            sender=message.sender,
            received_at=message.received_at,
            rfc822_message_id=message.rfc822_message_id,
            in_reply_to=message.in_reply_to,
            references_ids=list(message.references_ids),
            recipients=list(message.recipients),
            cc=list(message.cc),
            subject=message.subject,
            subject_normalized=message.subject_normalized,
            body_text=message.body_text,
            body_text_clean=message.body_text_clean,
            snippet=message.snippet,
            raw_object_key=message.raw_object_key,
            html_object_key=message.html_object_key,
            direction=message.direction,
            attachments=all_att_refs,
            normalization_failed=message.normalization_failed,
            signature_stripped=message.signature_stripped,
        )

        self.messages[(org_u, msg_u)] = msg_copy
        self.unique_provider_map[uniq_key] = msg_u

        # Persist attachments
        att_records: list[AttachmentRecord] = []
        now = datetime.now(UTC)
        for att in all_att_refs:
            att_rec = AttachmentRecord(
                id=uuid4(),
                organization_id=org_u,
                message_id=msg_u,
                filename=att.filename,
                mime_type=att.mime_type,
                size_bytes=att.size_bytes,
                object_key=att.object_key,
                checksum=att.checksum,
                created_at=now,
            )
            att_records.append(att_rec)
        self.attachments[(org_u, msg_u)] = att_records

        return MessageInsertResult(
            inserted=True,
            message_id=msg_u,
            thread_id=thd_u,
            is_duplicate=False,
        )

    async def get_message(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
    ) -> NormalizedMessage | None:
        org_u = _to_uuid(organization_id)
        msg_u = _to_uuid(message_id)
        return self.messages.get((org_u, msg_u))

    async def get_message_by_provider_id(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        provider_message_id: str,
    ) -> NormalizedMessage | None:
        org_u = _to_uuid(organization_id)
        mbx_u = _to_uuid(mailbox_id)
        uniq_key = (org_u, mbx_u, provider_message_id)
        msg_id = self.unique_provider_map.get(uniq_key)
        if msg_id:
            return self.messages.get((org_u, msg_id))
        return None

    async def get_attachments(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
    ) -> list[AttachmentRecord]:
        org_u = _to_uuid(organization_id)
        msg_u = _to_uuid(message_id)
        return list(self.attachments.get((org_u, msg_u), []))

    async def get_messages_by_thread(
        self,
        organization_id: UUID | str,
        thread_id: UUID | str,
    ) -> list[NormalizedMessage]:
        org_u = _to_uuid(organization_id)
        thd_u = _to_uuid(thread_id)
        matches = [
            m for m in self.messages.values() if m.organization_id == org_u and m.thread_id == thd_u
        ]
        matches.sort(key=lambda m: m.received_at)
        return matches

    async def search_messages_by_text(
        self,
        organization_id: UUID | str,
        query: str,
        limit: int = 20,
    ) -> list[NormalizedMessage]:
        org_u = _to_uuid(organization_id)
        tokens = [q.lower().strip() for q in query.split() if q.strip()]
        if not tokens:
            return []

        results: list[NormalizedMessage] = []
        for m in self.messages.values():
            if m.organization_id != org_u:
                continue
            haystack = f"{m.subject} {m.body_text_clean} {m.body_text}".lower()
            if any(token in haystack for token in tokens):
                results.append(m)
            if len(results) >= limit:
                break
        return results


class PostgresMessageStore:
    """PostgreSQL implementation of MessageStore with GIN index and ON CONFLICT DO NOTHING."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    def _serialize_recipients(self, recipients: Sequence[EmailAddress]) -> str:
        data = [{"name": r.name or "", "email": r.email} for r in recipients]
        return json.dumps(data)

    def _deserialize_recipients(self, raw: Any) -> list[EmailAddress]:
        if not raw:
            return []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return []
        if isinstance(raw, list):
            result: list[EmailAddress] = []
            for item in raw:
                if isinstance(item, dict):
                    result.append(
                        EmailAddress(
                            name=item.get("name") or None,
                            email=item.get("email", ""),
                        )
                    )
                elif isinstance(item, str):
                    result.append(EmailAddress(email=item))
            return result
        return []

    def _row_to_message(
        self,
        row: asyncpg.Record,
        attachments: list[AttachmentRef],
    ) -> NormalizedMessage:
        return NormalizedMessage(
            message_id=_to_uuid(row["id"]),
            thread_id=_to_uuid(row["thread_id"]),
            mailbox_id=_to_uuid(row["mailbox_id"]),
            organization_id=_to_uuid(row["organization_id"]),
            provider="unknown",  # Provider is mailbox-scoped
            provider_message_id=row["provider_message_id"],
            sender=EmailAddress(name=row["sender_name"], email=row["sender_email"] or ""),
            received_at=row["received_at"],
            rfc822_message_id=row["rfc822_message_id"],
            in_reply_to=row["in_reply_to"],
            references_ids=list(row["references_ids"]) if row["references_ids"] else [],
            recipients=self._deserialize_recipients(row["recipients"]),
            cc=self._deserialize_recipients(row["cc"]),
            subject=row["subject"] or "",
            subject_normalized=row["subject_normalized"] or "",
            body_text=row["body_text"] or "",
            body_text_clean=row["body_text_clean"] or "",
            snippet=row["snippet"] or "",
            raw_object_key=row["raw_object_key"],
            html_object_key=row["html_object_key"],
            direction=row["direction"],
            attachments=attachments,
            normalization_failed=bool(row["normalization_failed"]),
            signature_stripped=False,
        )

    async def insert_message(
        self,
        message: NormalizedMessage,
        attachments: Sequence[AttachmentRef] | None = None,
    ) -> MessageInsertResult:
        """Insert normalized message into email_message and attachments into attachment table.

        Uses ON CONFLICT (organization_id, mailbox_id, provider_message_id) DO NOTHING (R4.8).
        Populates search_tsv vector at write time per R5.6.
        """
        org_u = _to_uuid(message.organization_id)
        mbx_u = _to_uuid(message.mailbox_id)
        thd_u = _to_uuid(message.thread_id)
        msg_u = _to_uuid(message.message_id)

        all_att_refs: list[AttachmentRef] = list(message.attachments)
        if attachments:
            for att in attachments:
                if att not in all_att_refs:
                    all_att_refs.append(att)

        has_atts = len(all_att_refs) > 0
        recipients_json = self._serialize_recipients(message.recipients)
        cc_json = self._serialize_recipients(message.cc)
        references_list = list(message.references_ids) if message.references_ids else []

        query = """
            INSERT INTO email_message (
                id,
                organization_id,
                mailbox_id,
                thread_id,
                provider_message_id,
                rfc822_message_id,
                in_reply_to,
                references_ids,
                direction,
                sender_email,
                sender_name,
                recipients,
                cc,
                subject,
                subject_normalized,
                body_text,
                body_text_clean,
                snippet,
                raw_object_key,
                html_object_key,
                received_at,
                has_attachments,
                normalization_failed,
                search_tsv
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12::jsonb, $13::jsonb, $14, $15, $16, $17, $18, $19, $20,
                $21, $22, $23,
                setweight(to_tsvector('english', coalesce($14, '')), 'A')
                || setweight(to_tsvector('english', coalesce($17, '')), 'B')
            )
            ON CONFLICT (organization_id, mailbox_id, provider_message_id) DO NOTHING
            RETURNING id, thread_id;
        """

        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                query,
                msg_u,
                org_u,
                mbx_u,
                thd_u,
                message.provider_message_id,
                message.rfc822_message_id,
                message.in_reply_to,
                references_list,
                message.direction,
                message.sender.email if message.sender else None,
                message.sender.name if message.sender else None,
                recipients_json,
                cc_json,
                _clean_pg_str(message.subject),
                _clean_pg_str(message.subject_normalized),
                _clean_pg_str(message.body_text),
                _clean_pg_str(message.body_text_clean),
                _clean_pg_str(message.snippet),
                message.raw_object_key,
                message.html_object_key,
                message.received_at,
                has_atts,
                message.normalization_failed,
            )

            if not row:
                logger.info(
                    "Duplicate message rejected: org=%s, mailbox=%s, provider_msg_id=%s",
                    org_u,
                    mbx_u,
                    message.provider_message_id,
                )
                return MessageInsertResult(
                    inserted=False,
                    message_id=msg_u,
                    thread_id=thd_u,
                    is_duplicate=True,
                )

            # Persist attachments within same transaction
            if all_att_refs:
                att_query = """
                    INSERT INTO attachment (
                        id,
                        organization_id,
                        message_id,
                        filename,
                        mime_type,
                        size_bytes,
                        object_key,
                        checksum,
                        created_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now());
                """
                att_records_data = [
                    (
                        uuid4(),
                        org_u,
                        msg_u,
                        att.filename,
                        att.mime_type,
                        att.size_bytes,
                        att.object_key,
                        att.checksum,
                    )
                    for att in all_att_refs
                ]
                await conn.executemany(att_query, att_records_data)

            return MessageInsertResult(
                inserted=True,
                message_id=_to_uuid(row["id"]),
                thread_id=_to_uuid(row["thread_id"]),
                is_duplicate=False,
            )

    async def get_message(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
    ) -> NormalizedMessage | None:
        org_u = _to_uuid(organization_id)
        msg_u = _to_uuid(message_id)

        query = """
            SELECT * FROM email_message
            WHERE organization_id = $1 AND id = $2;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, org_u, msg_u)
            if not row:
                return None
            att_records = await self.get_attachments(org_u, msg_u)
            att_refs = [
                AttachmentRef(
                    filename=a.filename or "",
                    mime_type=a.mime_type or "application/octet-stream",
                    size_bytes=a.size_bytes or 0,
                    object_key=a.object_key,
                    checksum=a.checksum,
                )
                for a in att_records
            ]
            return self._row_to_message(row, att_refs)

    async def get_message_by_provider_id(
        self,
        organization_id: UUID | str,
        mailbox_id: UUID | str,
        provider_message_id: str,
    ) -> NormalizedMessage | None:
        org_u = _to_uuid(organization_id)
        mbx_u = _to_uuid(mailbox_id)

        query = """
            SELECT * FROM email_message
            WHERE organization_id = $1 AND mailbox_id = $2 AND provider_message_id = $3;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, org_u, mbx_u, provider_message_id)
            if not row:
                return None
            att_records = await self.get_attachments(org_u, row["id"])
            att_refs = [
                AttachmentRef(
                    filename=a.filename or "",
                    mime_type=a.mime_type or "application/octet-stream",
                    size_bytes=a.size_bytes or 0,
                    object_key=a.object_key,
                    checksum=a.checksum,
                )
                for a in att_records
            ]
            return self._row_to_message(row, att_refs)

    async def get_attachments(
        self,
        organization_id: UUID | str,
        message_id: UUID | str,
    ) -> list[AttachmentRecord]:
        org_u = _to_uuid(organization_id)
        msg_u = _to_uuid(message_id)

        query = """
            SELECT
                id, organization_id, message_id, filename,
                mime_type, size_bytes, object_key, checksum, created_at
            FROM attachment
            WHERE organization_id = $1 AND message_id = $2
            ORDER BY created_at ASC;
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, org_u, msg_u)
            return [
                AttachmentRecord(
                    id=_to_uuid(r["id"]),
                    organization_id=_to_uuid(r["organization_id"]),
                    message_id=_to_uuid(r["message_id"]),
                    filename=r["filename"],
                    mime_type=r["mime_type"],
                    size_bytes=r["size_bytes"],
                    object_key=r["object_key"],
                    checksum=r["checksum"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    async def get_messages_by_thread(
        self,
        organization_id: UUID | str,
        thread_id: UUID | str,
    ) -> list[NormalizedMessage]:
        org_u = _to_uuid(organization_id)
        thd_u = _to_uuid(thread_id)

        query = """
            SELECT * FROM email_message
            WHERE organization_id = $1 AND thread_id = $2
            ORDER BY received_at ASC;
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, org_u, thd_u)
            messages: list[NormalizedMessage] = []
            for row in rows:
                att_records = await self.get_attachments(org_u, row["id"])
                att_refs = [
                    AttachmentRef(
                        filename=a.filename or "",
                        mime_type=a.mime_type or "application/octet-stream",
                        size_bytes=a.size_bytes or 0,
                        object_key=a.object_key,
                        checksum=a.checksum,
                    )
                    for a in att_records
                ]
                messages.append(self._row_to_message(row, att_refs))
            return messages

    async def search_messages_by_text(
        self,
        organization_id: UUID | str,
        query: str,
        limit: int = 20,
    ) -> list[NormalizedMessage]:
        org_u = _to_uuid(organization_id)
        if not query.strip():
            return []

        search_query = """
            SELECT * FROM email_message
            WHERE organization_id = $1 AND search_tsv @@ plainto_tsquery('english', $2)
            ORDER BY ts_rank_cd(search_tsv, plainto_tsquery('english', $2)) DESC, received_at DESC
            LIMIT $3;
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(search_query, org_u, query.strip(), limit)
            results: list[NormalizedMessage] = []
            for row in rows:
                att_records = await self.get_attachments(org_u, row["id"])
                att_refs = [
                    AttachmentRef(
                        filename=a.filename or "",
                        mime_type=a.mime_type or "application/octet-stream",
                        size_bytes=a.size_bytes or 0,
                        object_key=a.object_key,
                        checksum=a.checksum,
                    )
                    for a in att_records
                ]
                results.append(self._row_to_message(row, att_refs))
            return results
