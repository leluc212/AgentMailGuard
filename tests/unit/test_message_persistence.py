"""Unit tests for message persistence and deduplication (R4.8, R5.4, R5.6, R4.7, R5.8)."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from packages.db.message import (
    AttachmentRecord,
    InMemoryMessageStore,
    MessageInsertResult,
)
from packages.db.thread import InMemoryThreadStore
from packages.domain.entities import (
    AttachmentRef,
    EmailAddress,
    NormalizedMessage,
)
from services.email_worker.persister import EmailPersister


def make_test_message(
    org_id: UUID | str | None = None,
    mailbox_id: UUID | str | None = None,
    msg_id: UUID | str | None = None,
    thread_id: UUID | str | None = None,
    provider_msg_id: str = "prov-msg-001",
    subject: str = "Quarterly Report Q3",
    body: str = "Please review the attached quarterly revenue numbers.",
    attachments: Sequence[AttachmentRef] | None = None,
) -> NormalizedMessage:
    o_id = UUID(str(org_id)) if org_id else uuid4()
    m_id = UUID(str(mailbox_id)) if mailbox_id else uuid4()
    msg_u = UUID(str(msg_id)) if msg_id else uuid4()
    thd_u = UUID(str(thread_id)) if thread_id else uuid4()
    return NormalizedMessage(
        message_id=msg_u,
        thread_id=thd_u,
        mailbox_id=m_id,
        organization_id=o_id,
        provider="gmail",
        provider_message_id=provider_msg_id,
        sender=EmailAddress(name="Alice Smith", email="alice@corp.com"),
        received_at=datetime(2026, 9, 17, 10, 0, 0, tzinfo=UTC),
        rfc822_message_id=f"<{provider_msg_id}@corp.com>",
        in_reply_to=None,
        references_ids=[],
        recipients=[EmailAddress(name="Bob Jones", email="bob@corp.com")],
        cc=[],
        subject=subject,
        subject_normalized=subject,
        body_text=body,
        body_text_clean=body,
        snippet=body[:50],
        raw_object_key="raw/msg1.eml",
        html_object_key="html/msg1.html",
        direction="inbound",
        attachments=list(attachments) if attachments else [],
        normalization_failed=False,
        signature_stripped=True,
    )


@pytest.mark.asyncio
async def test_in_memory_store_insert_and_get() -> None:
    store = InMemoryMessageStore()
    att = AttachmentRef(
        filename="report.pdf",
        mime_type="application/pdf",
        size_bytes=10240,
        object_key="attachments/report.pdf",
        checksum="sha256:abcd",
    )
    msg = make_test_message(attachments=[att])

    result = await store.insert_message(msg)
    assert isinstance(result, MessageInsertResult)
    assert result.inserted is True
    assert result.is_duplicate is False
    assert result.message_id == msg.message_id
    assert result.thread_id == msg.thread_id

    # Retrieve by ID
    retrieved = await store.get_message(msg.organization_id, msg.message_id)
    assert retrieved is not None
    assert retrieved.message_id == msg.message_id
    assert retrieved.subject == "Quarterly Report Q3"
    assert len(retrieved.attachments) == 1
    assert retrieved.attachments[0].filename == "report.pdf"

    # Retrieve by provider message ID
    by_prov = await store.get_message_by_provider_id(
        msg.organization_id, msg.mailbox_id, msg.provider_message_id
    )
    assert by_prov is not None
    assert by_prov.message_id == msg.message_id

    # Check attachments records
    att_records = await store.get_attachments(msg.organization_id, msg.message_id)
    assert len(att_records) == 1
    assert isinstance(att_records[0], AttachmentRecord)
    assert att_records[0].filename == "report.pdf"
    assert att_records[0].object_key == "attachments/report.pdf"


@pytest.mark.asyncio
async def test_in_memory_store_deduplication_on_conflict() -> None:
    store = InMemoryMessageStore()
    org_id = uuid4()
    mbx_id = uuid4()
    msg1 = make_test_message(
        org_id=org_id,
        mailbox_id=mbx_id,
        provider_msg_id="replayed-msg-999",
    )

    res1 = await store.insert_message(msg1)
    assert res1.inserted is True
    assert res1.is_duplicate is False

    # Second insert with identical org_id, mailbox_id, provider_message_id
    msg2 = make_test_message(
        org_id=org_id,
        mailbox_id=mbx_id,
        msg_id=uuid4(),  # Different local message UUID
        provider_msg_id="replayed-msg-999",
    )
    res2 = await store.insert_message(msg2)
    assert res2.inserted is False
    assert res2.is_duplicate is True
    assert res2.message_id == msg1.message_id  # Returns original message ID


@pytest.mark.asyncio
async def test_in_memory_store_tenant_isolation() -> None:
    store = InMemoryMessageStore()
    mbx_id = uuid4()
    prov_id = "shared-id-123"

    org1 = uuid4()
    org2 = uuid4()

    msg1 = make_test_message(org_id=org1, mailbox_id=mbx_id, provider_msg_id=prov_id)
    msg2 = make_test_message(org_id=org2, mailbox_id=mbx_id, provider_msg_id=prov_id)

    # Same provider ID across two different tenants must both succeed
    res1 = await store.insert_message(msg1)
    res2 = await store.insert_message(msg2)

    assert res1.inserted is True
    assert res2.inserted is True

    # Tenant 1 cannot access Tenant 2 message
    assert await store.get_message(org1, msg2.message_id) is None
    assert await store.get_message(org2, msg1.message_id) is None


@pytest.mark.asyncio
async def test_in_memory_store_thread_messages_and_search() -> None:
    store = InMemoryMessageStore()
    org_id = uuid4()
    mbx_id = uuid4()
    thd_id = uuid4()

    msg1 = make_test_message(
        org_id=org_id,
        mailbox_id=mbx_id,
        thread_id=thd_id,
        provider_msg_id="m1",
        subject="Invoice #1001",
        body="Attached invoice for service rendered.",
    )
    msg2 = make_test_message(
        org_id=org_id,
        mailbox_id=mbx_id,
        thread_id=thd_id,
        provider_msg_id="m2",
        subject="Re: Invoice #1001",
        body="Payment has been scheduled for tomorrow.",
    )
    msg2.received_at = datetime(2026, 9, 17, 11, 0, 0, tzinfo=UTC)

    await store.insert_message(msg1)
    await store.insert_message(msg2)

    # Thread messages
    thread_msgs = await store.get_messages_by_thread(org_id, thd_id)
    assert len(thread_msgs) == 2
    assert thread_msgs[0].message_id == msg1.message_id
    assert thread_msgs[1].message_id == msg2.message_id

    # Text search
    search_invoice = await store.search_messages_by_text(org_id, "Invoice")
    assert len(search_invoice) == 2

    search_scheduled = await store.search_messages_by_text(org_id, "scheduled")
    assert len(search_scheduled) == 1
    assert search_scheduled[0].message_id == msg2.message_id


@pytest.mark.asyncio
async def test_persister_pipeline_first_time_message() -> None:
    message_store = InMemoryMessageStore()
    thread_store = InMemoryThreadStore()
    persister = EmailPersister(message_store=message_store, thread_store=thread_store)

    msg = make_test_message(
        subject="Project Kickoff",
        body="Welcome team to project kickoff.",
    )

    result = await persister.persist(msg)
    assert result.success is True
    assert result.is_duplicate is False
    assert result.should_dispatch is True
    assert result.thread is not None
    assert result.thread.message_count == 1
    assert result.message.thread_id == result.thread.id

    # Check that message and thread exist in stores
    assert await message_store.get_message(msg.organization_id, msg.message_id) is not None
    assert await thread_store.get_thread(msg.organization_id, result.thread.id) is not None


@pytest.mark.asyncio
async def test_persister_pipeline_replayed_message_suppresses_dispatch() -> None:
    message_store = InMemoryMessageStore()
    thread_store = InMemoryThreadStore()
    persister = EmailPersister(message_store=message_store, thread_store=thread_store)

    msg1 = make_test_message(
        provider_msg_id="replayed-stream-001",
        subject="Password Reset Request",
        body="Please reset my corporate password.",
    )

    res1 = await persister.persist(msg1)
    assert res1.success is True
    assert res1.is_duplicate is False
    assert res1.should_dispatch is True
    assert res1.thread is not None
    orig_thread_id = res1.thread.id

    # Replay same message payload (e.g. broker redelivery or webhook retry)
    msg2 = make_test_message(
        org_id=msg1.organization_id,
        mailbox_id=msg1.mailbox_id,
        msg_id=uuid4(),  # New delivery envelope UUID
        provider_msg_id="replayed-stream-001",
        subject="Password Reset Request",
        body="Please reset my corporate password.",
    )

    res2 = await persister.persist(msg2)
    assert res2.success is True
    assert res2.is_duplicate is True
    assert res2.should_dispatch is False  # R4.8: DO NOT create a second job
    assert res2.thread is not None
    assert res2.thread.id == orig_thread_id

    # Confirm thread counters were NOT incremented a second time
    thread_check = await thread_store.get_thread(msg1.organization_id, orig_thread_id)
    assert thread_check is not None
    assert thread_check.message_count == 1
