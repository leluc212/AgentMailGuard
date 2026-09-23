"""Unit tests for Job timeline API and operator replay (R18.6, R18.7, R23.2, R23.5, R23.6)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from packages.broker.envelope import JobEnvelope
from packages.db.job import InMemoryJobStore
from packages.db.message import InMemoryMessageStore
from packages.domain.entities import EmailAddress, Job, NormalizedMessage, ProcessingEvent
from packages.domain.state_machine import JobState
from services.api.main import create_app


@pytest.fixture
def org_a() -> UUID:
    return uuid4()


@pytest.fixture
def org_b() -> UUID:
    return uuid4()


@pytest.fixture
def mock_publisher() -> MagicMock:
    pub = MagicMock()
    pub.publish = AsyncMock()
    pub.broker_settings = MagicMock()
    pub.broker_settings.exchange_email_route = "email.events"
    return pub


@pytest.fixture
def test_app(mock_publisher: MagicMock) -> FastAPI:
    """Create test FastAPI application with in-memory stores attached to app.state."""
    app = create_app(lifespan_enabled=False)
    app.state.job_store = InMemoryJobStore()
    app.state.message_store = InMemoryMessageStore()
    app.state.publisher = mock_publisher
    return app


@pytest.fixture
async def client(test_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _create_sample_message(org_id: UUID, message_id: UUID) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=message_id,
        organization_id=org_id,
        mailbox_id=uuid4(),
        thread_id=uuid4(),
        provider="gmail",
        provider_message_id=f"prov-{message_id}",
        sender=EmailAddress(name="Alice", email="alice@corp.com"),
        recipients=[EmailAddress(name="Support", email="support@corp.com")],
        subject="Issue with server",
        subject_normalized="issue with server",
        body_text="Server is down",
        body_text_clean="Server is down",
        snippet="Server is down",
        received_at=datetime.now(UTC),
    )


class TestMessageTimelineAPI:
    """Validate GET /v1/messages/{id}/timeline (R18.6, R23.5, R23.6)."""

    @pytest.mark.asyncio
    async def test_message_timeline_chronological_ordering_and_state(
        self, test_app: FastAPI, client: AsyncClient, org_a: UUID
    ) -> None:
        msg_store: InMemoryMessageStore = test_app.state.message_store
        job_store: InMemoryJobStore = test_app.state.job_store

        msg_id = uuid4()
        job_id = uuid4()
        msg = _create_sample_message(org_a, msg_id)
        await msg_store.insert_message(msg)

        now = datetime.now(UTC)
        # Create a series of chronological events
        states = [
            ("RECEIVED", "NORMALIZED", 0),
            ("NORMALIZED", "CLASSIFIED", 1),
            ("CLASSIFIED", "QUEUED", 2),
            ("QUEUED", "CONTEXT_READY", 3),
            ("CONTEXT_READY", "GENERATING", 4),
        ]
        for from_s, to_s, offset_sec in states:
            ev = ProcessingEvent(
                id=offset_sec + 1,
                job_id=job_id,
                message_id=msg_id,
                organization_id=org_a,
                event_type="state_transition",
                state_from=from_s,
                state_to=to_s,
                payload={"step": offset_sec},
                created_at=now + timedelta(seconds=offset_sec),
            )
            job_store._events.append(ev)

        response = await client.get(
            f"/v1/messages/{msg_id}/timeline",
            headers={"X-Organization-ID": str(org_a)},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        assert data["message_id"] == str(msg_id)
        assert data["organization_id"] == str(org_a)
        assert data["current_state"] == "GENERATING"
        assert data["total_events"] == 5
        assert len(data["events"]) == 5

        # Verify ordering
        assert [e["state_to"] for e in data["events"]] == [
            "NORMALIZED",
            "CLASSIFIED",
            "QUEUED",
            "CONTEXT_READY",
            "GENERATING",
        ]

    @pytest.mark.asyncio
    async def test_message_timeline_pagination(
        self, test_app: FastAPI, client: AsyncClient, org_a: UUID
    ) -> None:
        msg_store: InMemoryMessageStore = test_app.state.message_store
        job_store: InMemoryJobStore = test_app.state.job_store

        msg_id = uuid4()
        msg = _create_sample_message(org_a, msg_id)
        await msg_store.insert_message(msg)

        for i in range(10):
            job_store._events.append(
                ProcessingEvent(
                    id=i + 1,
                    job_id=uuid4(),
                    message_id=msg_id,
                    organization_id=org_a,
                    event_type="state_transition",
                    state_from="QUEUED",
                    state_to="GENERATING",
                    payload={"index": i},
                    created_at=datetime.now(UTC) + timedelta(seconds=i),
                )
            )

        # Page 1: limit 3, offset 0
        p1 = await client.get(
            f"/v1/messages/{msg_id}/timeline?limit=3&offset=0",
            headers={"X-Organization-ID": str(org_a)},
        )
        assert p1.status_code == status.HTTP_200_OK
        p1_data = p1.json()
        assert p1_data["total_events"] == 10
        assert len(p1_data["events"]) == 3
        assert [e["payload"]["index"] for e in p1_data["events"]] == [0, 1, 2]

        # Page 2: limit 3, offset 3
        p2 = await client.get(
            f"/v1/messages/{msg_id}/timeline?limit=3&offset=3",
            headers={"X-Organization-ID": str(org_a)},
        )
        assert p2.status_code == status.HTTP_200_OK
        p2_data = p2.json()
        assert len(p2_data["events"]) == 3
        assert [e["payload"]["index"] for e in p2_data["events"]] == [3, 4, 5]

    @pytest.mark.asyncio
    async def test_message_timeline_not_found(
        self, client: AsyncClient, org_a: UUID
    ) -> None:
        missing_id = uuid4()
        response = await client.get(
            f"/v1/messages/{missing_id}/timeline",
            headers={"X-Organization-ID": str(org_a)},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["code"] == "MESSAGE_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_message_timeline_tenant_isolation(
        self, test_app: FastAPI, client: AsyncClient, org_a: UUID, org_b: UUID
    ) -> None:
        msg_store: InMemoryMessageStore = test_app.state.message_store
        msg_id = uuid4()
        msg = _create_sample_message(org_a, msg_id)
        await msg_store.insert_message(msg)

        # Org B querying Org A message should receive 404
        response = await client.get(
            f"/v1/messages/{msg_id}/timeline",
            headers={"X-Organization-ID": str(org_b)},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_message_timeline_missing_tenant_header(
        self, client: AsyncClient
    ) -> None:
        response = await client.get(f"/v1/messages/{uuid4()}/timeline")
        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestJobEndpointsAndReplay:
    """Validate GET /v1/jobs/{id}, GET /v1/jobs/{id}/timeline, and POST /v1/jobs/{id}/replay.

    Requirements: R18.7, R23.2.
    """

    @pytest.mark.asyncio
    async def test_get_job_detail_and_timeline(
        self, test_app: FastAPI, client: AsyncClient, org_a: UUID
    ) -> None:
        job_store: InMemoryJobStore = test_app.state.job_store
        job_id = uuid4()
        job = Job(
            id=job_id,
            organization_id=org_a,
            message_id=uuid4(),
            job_type="generate_reply",
            state=JobState.QUEUED.value,
            queue_name="email.support.normal",
            priority="normal",
            idempotency_key=f"idem-{job_id}",
        )
        await job_store.create_job(job)

        # 1. Test get_job
        res = await client.get(
            f"/v1/jobs/{job_id}",
            headers={"X-Organization-ID": str(org_a)},
        )
        assert res.status_code == status.HTTP_200_OK
        job_data = res.json()
        assert job_data["id"] == str(job_id)
        assert job_data["state"] == "QUEUED"
        assert job_data["queue_name"] == "email.support.normal"

        # 2. Test get_job_timeline
        res_tl = await client.get(
            f"/v1/jobs/{job_id}/timeline",
            headers={"X-Organization-ID": str(org_a)},
        )
        assert res_tl.status_code == status.HTTP_200_OK
        tl_data = res_tl.json()
        assert tl_data["job_id"] == str(job_id)
        assert tl_data["total_events"] == 1
        assert tl_data["events"][0]["state_to"] == "QUEUED"

    @pytest.mark.asyncio
    async def test_replay_dead_letter_job_success(
        self, test_app: FastAPI, client: AsyncClient, org_a: UUID, mock_publisher: MagicMock
    ) -> None:
        """Verify operator replay transitions DEAD_LETTER -> RETRY_PENDING and republishes.

        Requirements: R18.7.
        """
        job_store: InMemoryJobStore = test_app.state.job_store
        job_id = uuid4()
        msg_id = uuid4()

        # Seed dead-lettered job
        job = Job(
            id=job_id,
            organization_id=org_a,
            message_id=msg_id,
            job_type="generate_reply",
            state=JobState.DEAD_LETTER.value,
            attempt=5,
            max_attempts=5,
            queue_name="email.support.normal",
            last_error="Max retry attempts exceeded",
            idempotency_key=f"idem-{job_id}",
        )
        await job_store.create_job(job)

        response = await client.post(
            f"/v1/jobs/{job_id}/replay",
            headers={"X-Organization-ID": str(org_a)},
            json={"reset_attempts": True, "reason": "Operator manually unblocked pipeline"},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        assert data["job_id"] == str(job_id)
        assert data["organization_id"] == str(org_a)
        assert data["previous_state"] == "DEAD_LETTER"
        assert data["new_state"] == "RETRY_PENDING"
        assert data["attempt"] == 0
        assert data["republished"] is True
        assert data["routing_key"] == "email.support.normal"

        # Verify job updated in job_store
        updated_job = await job_store.get_job(org_a, job_id)
        assert updated_job is not None
        assert updated_job.state == "RETRY_PENDING"
        assert updated_job.attempt == 0
        assert updated_job.last_error is None

        # Verify mock publisher was invoked with expected envelope
        mock_publisher.publish.assert_awaited_once()
        _, kwargs = mock_publisher.publish.call_args
        envelope: JobEnvelope = kwargs["envelope"]
        assert envelope.job_id == str(job_id)
        assert envelope.organization_id == str(org_a)
        assert envelope.payload["replayed"] is True
        assert envelope.payload["reason"] == "Operator manually unblocked pipeline"

    @pytest.mark.asyncio
    async def test_replay_dead_letter_job_without_attempt_reset(
        self, test_app: FastAPI, client: AsyncClient, org_a: UUID
    ) -> None:
        """Verify operator replay with reset_attempts=False preserves attempt counter."""
        job_store: InMemoryJobStore = test_app.state.job_store
        job_id = uuid4()

        job = Job(
            id=job_id,
            organization_id=org_a,
            job_type="generate_reply",
            state=JobState.DEAD_LETTER.value,
            attempt=3,
            max_attempts=5,
            idempotency_key=f"idem-{job_id}",
        )
        await job_store.create_job(job)

        response = await client.post(
            f"/v1/jobs/{job_id}/replay",
            headers={"X-Organization-ID": str(org_a)},
            json={"reset_attempts": False, "reason": "Operator retry preserving attempt count"},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["attempt"] == 3

        updated_job = await job_store.get_job(org_a, job_id)
        assert updated_job is not None
        assert updated_job.attempt == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "invalid_state",
        [
            JobState.GENERATING.value,
            JobState.COMPLETED.value,
            JobState.QUEUED.value,
            JobState.CONTEXT_READY.value,
        ],
    )
    async def test_replay_rejects_non_dead_letter_states(
        self, test_app: FastAPI, client: AsyncClient, org_a: UUID, invalid_state: str
    ) -> None:
        """Verify replay returns 409 Conflict when job is not in DEAD_LETTER state (R18.7)."""
        job_store: InMemoryJobStore = test_app.state.job_store
        job_id = uuid4()

        job = Job(
            id=job_id,
            organization_id=org_a,
            job_type="generate_reply",
            state=invalid_state,
            idempotency_key=f"idem-{job_id}",
        )
        await job_store.create_job(job)

        response = await client.post(
            f"/v1/jobs/{job_id}/replay",
            headers={"X-Organization-ID": str(org_a)},
            json={"reason": "Premature replay"},
        )
        assert response.status_code == status.HTTP_409_CONFLICT
        data = response.json()
        assert data["code"] == "JOB_NOT_REPLAYABLE"
        assert data["detail"]["current_state"] == invalid_state

    @pytest.mark.asyncio
    async def test_replay_job_not_found(
        self, client: AsyncClient, org_a: UUID
    ) -> None:
        missing_id = uuid4()
        response = await client.post(
            f"/v1/jobs/{missing_id}/replay",
            headers={"X-Organization-ID": str(org_a)},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["code"] == "JOB_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_replay_job_tenant_isolation(
        self, test_app: FastAPI, client: AsyncClient, org_a: UUID, org_b: UUID
    ) -> None:
        job_store: InMemoryJobStore = test_app.state.job_store
        job_id = uuid4()

        job = Job(
            id=job_id,
            organization_id=org_a,
            job_type="generate_reply",
            state=JobState.DEAD_LETTER.value,
            idempotency_key=f"idem-{job_id}",
        )
        await job_store.create_job(job)

        # Org B attempting to replay Org A's job gets 404
        response = await client.post(
            f"/v1/jobs/{job_id}/replay",
            headers={"X-Organization-ID": str(org_b)},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
