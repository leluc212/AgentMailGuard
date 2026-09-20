"""Unit tests for subject normalization and multi-tier thread association.

Requirements:
- R4.5: Normalize subjects by stripping reply/forward prefixes for thread matching.
- R4.6: Associate each message with a thread using provider thread id when available,
        else In-Reply-To/References, else normalized subject + participant set within window.
- Maintain email_thread counters, timestamps, and participant sets.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from packages.db.thread import InMemoryThreadStore
from packages.domain.entities import EmailAddress, EmailThread, NormalizedMessage
from services.email_worker.parser import normalize_subject
from services.email_worker.threading import (
    ThreadAssociationReason,
    ThreadAssociator,
    extract_participant_emails,
)


class TestSubjectNormalization:
    """Tests for pure subject normalization stripping reply/forward prefixes (R4.5)."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Re: Project Alpha", "Project Alpha"),
            ("RE: Project Alpha", "Project Alpha"),
            ("re: project alpha", "project alpha"),
            ("Fwd: Project Alpha", "Project Alpha"),
            ("FW: Project Alpha", "Project Alpha"),
            ("fwd: project alpha", "project alpha"),
            ("Aw: Project Alpha", "Project Alpha"),
            ("Tr: Project Alpha", "Project Alpha"),
            ("Sv: Project Alpha", "Project Alpha"),
            ("Vs: Project Alpha", "Project Alpha"),
            ("Re[2]: Project Alpha", "Project Alpha"),
            ("Re[12]: Project Alpha", "Project Alpha"),
            ("Re: Fwd: Re: Aw: Multi Stacked", "Multi Stacked"),
            ("   Re:   Spaced    Title   ", "Spaced Title"),
            ("Clean Subject Line Without Prefix", "Clean Subject Line Without Prefix"),
            ("", ""),
        ],
    )
    def test_strip_prefixes(self, raw: str, expected: str) -> None:
        assert normalize_subject(raw) == expected


class TestParticipantExtraction:
    """Tests for participant email address extraction and normalization."""

    def test_extract_and_deduplicate_participants(self) -> None:
        sender = EmailAddress("ALICE@example.com", "Alice Smith")
        recipients: list[EmailAddress | str] = [
            EmailAddress("bob@example.com", "Bob"),
            "Charlie <charlie@EXAMPLE.COM>",
        ]
        cc = ["alice@example.com", "dave@example.com"]

        participants = extract_participant_emails(sender, recipients, cc)
        assert participants == [
            "alice@example.com",
            "bob@example.com",
            "charlie@example.com",
            "dave@example.com",
        ]

    def test_handles_empty_participants(self) -> None:
        assert extract_participant_emails(None, None, None) == []
        assert extract_participant_emails("", [], []) == []


class TestThreadAssociatorHierarchy:
    """Tests validating the 4-tier thread association cascade (R4.6, design.md §5.2)."""

    @pytest.fixture
    def store(self) -> InMemoryThreadStore:
        return InMemoryThreadStore()

    @pytest.fixture
    def associator(self, store: InMemoryThreadStore) -> ThreadAssociator:
        return ThreadAssociator(store=store, default_window_days=14)

    @pytest.mark.asyncio
    async def test_tier_1_provider_thread_id_match(
        self,
        store: InMemoryThreadStore,
        associator: ThreadAssociator,
    ) -> None:
        org_id = uuid4()
        mbx_id = uuid4()
        prov_thread_id = "thread_google_999"
        existing_thread_id = uuid4()

        # Seed existing thread
        initial_thread = EmailThread(
            id=existing_thread_id,
            organization_id=org_id,
            mailbox_id=mbx_id,
            subject_normalized="Quarterly Budget",
            provider_thread_id=prov_thread_id,
            participants=["alice@example.com"],
            message_count=1,
        )
        await store.create_thread(initial_thread)

        # Incoming message with matching provider_thread_id
        res = await associator.associate_message(
            organization_id=org_id,
            mailbox_id=mbx_id,
            subject_normalized="Completely Different Subject",
            sender=EmailAddress("bob@example.com"),
            recipients=[EmailAddress("alice@example.com")],
            provider_thread_id=prov_thread_id,
        )

        assert res.is_new is False
        assert res.reason == ThreadAssociationReason.PROVIDER_THREAD_ID
        assert res.thread_id == existing_thread_id
        assert res.thread.message_count == 2
        assert "bob@example.com" in res.thread.participants

    @pytest.mark.asyncio
    async def test_tier_2a_in_reply_to_match(
        self,
        store: InMemoryThreadStore,
        associator: ThreadAssociator,
    ) -> None:
        org_id = uuid4()
        mbx_id = uuid4()
        parent_msg_rfc_id = "parent_msg_001@example.com"
        parent_thread_id = uuid4()

        # Seed parent message pointing to parent_thread_id
        store.register_message(org_id, mbx_id, parent_msg_rfc_id, parent_thread_id)
        parent_thread = EmailThread(
            id=parent_thread_id,
            organization_id=org_id,
            mailbox_id=mbx_id,
            subject_normalized="API Redesign",
            participants=["architect@example.com"],
            message_count=1,
        )
        await store.create_thread(parent_thread)

        # Incoming reply with In-Reply-To
        res = await associator.associate_message(
            organization_id=org_id,
            mailbox_id=mbx_id,
            subject_normalized="Re: API Redesign",
            sender=EmailAddress("dev@example.com"),
            recipients=[EmailAddress("architect@example.com")],
            in_reply_to=f"<{parent_msg_rfc_id}>",
        )

        assert res.is_new is False
        assert res.reason == ThreadAssociationReason.IN_REPLY_TO
        assert res.thread_id == parent_thread_id
        assert res.thread.message_count == 2
        assert "dev@example.com" in res.thread.participants

    @pytest.mark.asyncio
    async def test_tier_2b_references_match_picks_latest(
        self,
        store: InMemoryThreadStore,
        associator: ThreadAssociator,
    ) -> None:
        org_id = uuid4()
        mbx_id = uuid4()
        thread_target = uuid4()
        ref1 = "ref_root@example.com"
        ref2 = "ref_turn2@example.com"

        store.register_message(org_id, mbx_id, ref2, thread_target)
        target_thread = EmailThread(
            id=thread_target,
            organization_id=org_id,
            mailbox_id=mbx_id,
            subject_normalized="Database Migration",
            participants=["dba@example.com"],
            message_count=2,
        )
        await store.create_thread(target_thread)

        # Incoming message with References list
        res = await associator.associate_message(
            organization_id=org_id,
            mailbox_id=mbx_id,
            subject_normalized="Database Migration",
            sender=EmailAddress("lead@example.com"),
            recipients=[EmailAddress("dba@example.com")],
            references_ids=[f"<{ref1}>", f"<{ref2}>"],
        )

        assert res.is_new is False
        assert res.reason == ThreadAssociationReason.REFERENCES
        assert res.thread_id == thread_target
        assert res.thread.message_count == 3

    @pytest.mark.asyncio
    async def test_tier_3_subject_and_overlapping_participants_within_window(
        self,
        store: InMemoryThreadStore,
        associator: ThreadAssociator,
    ) -> None:
        org_id = uuid4()
        mbx_id = uuid4()
        existing_thread_id = uuid4()
        now = datetime.now(UTC)

        # Existing thread active 3 days ago
        recent_thread = EmailThread(
            id=existing_thread_id,
            organization_id=org_id,
            mailbox_id=mbx_id,
            subject_normalized="Security Incident 404",
            participants=["alice@corp.com", "bob@corp.com"],
            first_message_at=now - timedelta(days=5),
            last_message_at=now - timedelta(days=3),
            message_count=2,
        )
        await store.create_thread(recent_thread)

        # Incoming message with same normalized subject and overlapping participant
        res = await associator.associate_message(
            organization_id=org_id,
            mailbox_id=mbx_id,
            subject_normalized="Security Incident 404",
            sender=EmailAddress("bob@corp.com"),
            recipients=[EmailAddress("carol@corp.com")],
            received_at=now,
            window_days=14,
        )

        assert res.is_new is False
        assert res.reason == ThreadAssociationReason.SUBJECT_PARTICIPANTS
        assert res.thread_id == existing_thread_id
        assert res.thread.message_count == 3
        assert "carol@corp.com" in res.thread.participants
        assert res.thread.last_message_at == now

    @pytest.mark.asyncio
    async def test_tier_3_fails_when_outside_time_window(
        self,
        store: InMemoryThreadStore,
        associator: ThreadAssociator,
    ) -> None:
        org_id = uuid4()
        mbx_id = uuid4()
        now = datetime.now(UTC)

        # Thread older than window (20 days ago, window is 14 days)
        old_thread = EmailThread(
            id=uuid4(),
            organization_id=org_id,
            mailbox_id=mbx_id,
            subject_normalized="Pricing Inquiry",
            participants=["buyer@client.com"],
            first_message_at=now - timedelta(days=25),
            last_message_at=now - timedelta(days=20),
            message_count=1,
        )
        await store.create_thread(old_thread)

        res = await associator.associate_message(
            organization_id=org_id,
            mailbox_id=mbx_id,
            subject_normalized="Pricing Inquiry",
            sender=EmailAddress("buyer@client.com"),
            recipients=[EmailAddress("sales@company.com")],
            received_at=now,
            window_days=14,
        )

        # Outside window -> must create a NEW thread
        assert res.is_new is True
        assert res.reason == ThreadAssociationReason.NEW_THREAD
        assert res.thread_id != old_thread.id

    @pytest.mark.asyncio
    async def test_tier_3_fails_when_participants_are_disjoint(
        self,
        store: InMemoryThreadStore,
        associator: ThreadAssociator,
    ) -> None:
        org_id = uuid4()
        mbx_id = uuid4()
        now = datetime.now(UTC)

        # Thread with participants Alice & Bob
        thread1 = EmailThread(
            id=uuid4(),
            organization_id=org_id,
            mailbox_id=mbx_id,
            subject_normalized="Order Status",
            participants=["alice@test.com", "bob@test.com"],
            last_message_at=now - timedelta(days=1),
        )
        await store.create_thread(thread1)

        # Incoming message with same subject but completely disjoint participants (Carol & Dave)
        res = await associator.associate_message(
            organization_id=org_id,
            mailbox_id=mbx_id,
            subject_normalized="Order Status",
            sender=EmailAddress("carol@other.com"),
            recipients=[EmailAddress("dave@other.com")],
            received_at=now,
            window_days=14,
        )

        # Disjoint participants -> must create a NEW thread
        assert res.is_new is True
        assert res.reason == ThreadAssociationReason.NEW_THREAD
        assert res.thread_id != thread1.id

    @pytest.mark.asyncio
    async def test_tier_4_new_thread_created(
        self,
        associator: ThreadAssociator,
    ) -> None:
        org_id = uuid4()
        mbx_id = uuid4()

        res = await associator.associate_message(
            organization_id=org_id,
            mailbox_id=mbx_id,
            subject_normalized="Brand New Discussion",
            sender=EmailAddress("founder@startup.io"),
            recipients=[EmailAddress("investor@fund.com")],
        )

        assert res.is_new is True
        assert res.reason == ThreadAssociationReason.NEW_THREAD
        assert res.thread.message_count == 1
        assert res.thread.subject_normalized == "Brand New Discussion"
        assert res.thread.participants == ["founder@startup.io", "investor@fund.com"]

    @pytest.mark.asyncio
    async def test_associate_normalized_message_mutates_thread_id(
        self,
        associator: ThreadAssociator,
    ) -> None:
        msg = NormalizedMessage(
            message_id=uuid4(),
            thread_id=uuid4(),  # Initial placeholder thread_id
            mailbox_id=uuid4(),
            organization_id=uuid4(),
            provider="gmail",
            provider_message_id="msg_gmail_01",
            sender=EmailAddress("sender@test.com"),
            received_at=datetime.now(UTC),
            subject="Welcome!",
            subject_normalized="Welcome!",
            recipients=[EmailAddress("user@test.com")],
        )

        result = await associator.associate_normalized_message(msg)
        assert msg.thread_id == result.thread_id
        assert result.is_new is True
