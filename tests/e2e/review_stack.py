"""A real review UI + real /v1 API on the isolated test database, for browser tests (6.8).

The frontend (services.frontend) serves on 127.0.0.1 from a background uvicorn thread; its
/v1 client runs in-process (httpx.ASGITransport) against services.api with the asyncpg pool
of the rag_email_test database. Only the broker edge is recorded instead of published:
the approve -> email.dispatch publish itself is asserted by the 6.1 integration tests.
Database seeding and assertions run on the stack's own event loop (run()), so the test
thread never runs asyncio while Playwright's sync API is active. No credentials (R24.5).
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import uuid
from collections.abc import Coroutine
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, TypeVar

import asyncpg
import httpx
import uvicorn

from packages.broker.envelope import JobEnvelope
from packages.core.settings import (
    AppSettings,
    BrokerSettings,
    FrontendServiceSettings,
    FrontendSettings,
)
from packages.db.connection import create_pool_from_settings
from packages.db.draft import PostgresDraftStore
from packages.db.job import PostgresJobStore
from packages.domain.entities import GeneratedDraft, Job
from packages.domain.state_machine import JobState
from services.api.main import create_app as create_api_app
from services.frontend.main import create_app as create_frontend_app

T = TypeVar("T")

SUBJECT = "Re: Where is order ORD-82915?"
DRAFT_BODY = (
    "Your order ORD-82915 was dispatched on 26 September and should arrive within two "
    "working days. If anything is missing, reply to this email and we will help."
)
THREAD_SUMMARY = "Alice asks for the delivery status of ORD-82915."


class RecordingPublisher:
    """Stands in for MessagePublisher at the broker edge; records what approve publishes."""

    def __init__(self, settings: BrokerSettings) -> None:
        self.settings = settings
        self.published: list[tuple[str, str, JobEnvelope]] = []

    async def publish(
        self,
        exchange_name: str,
        routing_key: str,
        envelope: JobEnvelope,
        headers: dict[str, Any] | None = None,
    ) -> None:
        self.published.append((exchange_name, routing_key, envelope))


@dataclass(frozen=True)
class SeededDraft:
    draft_id: uuid.UUID
    job_id: uuid.UUID
    message_id: uuid.UUID


@dataclass
class ReviewOutcome:
    draft_status: str
    draft_body: str
    feedback: list[dict[str, Any]] = field(default_factory=list)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


class ReviewStack:
    """Frontend on http://127.0.0.1:<port> wired to the real API on the test database."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self.loop.run_forever, name="review-ui-stack", daemon=True
        )
        self.organization_id = uuid.uuid4()
        self.publisher = RecordingPublisher(AppSettings().broker)
        self.url = ""
        self._pool: asyncpg.Pool | None = None
        self._server: uvicorn.Server | None = None
        self._serving: Future[None] | None = None

    @property
    def db(self) -> asyncpg.Pool:
        assert self._pool is not None, "ReviewStack.start() has not run"
        return self._pool

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=30)

    def start(self) -> None:
        self._thread.start()
        self._pool = self.run(create_pool_from_settings(AppSettings().database))
        api_app = create_api_app(lifespan_enabled=False)
        api_app.state.db_pool = self._pool
        api_app.state.publisher = self.publisher
        settings = FrontendServiceSettings(
            _env_file=None,
            frontend=FrontendSettings(
                api_base_url="http://api", organization_id=self.organization_id
            ),
        )
        frontend = create_frontend_app(
            settings, transport=httpx.ASGITransport(app=api_app), configure_logging=False
        )
        port = _free_port()
        config = uvicorn.Config(
            frontend, host="127.0.0.1", port=port, log_level="warning", log_config=None
        )
        self._server = uvicorn.Server(config)
        self._serving = asyncio.run_coroutine_threadsafe(self._server.serve(), self.loop)
        deadline = time.monotonic() + 10
        while not self._server.started:
            if self._serving.done():
                self._serving.result()  # surfaces a startup error
            if time.monotonic() > deadline:
                raise RuntimeError("review UI did not start within 10 s")
            time.sleep(0.05)
        self.url = f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        if self._server is not None and self._serving is not None:
            self._server.should_exit = True
            self._serving.result(timeout=10)
        if self._pool is not None:
            self.run(self._pool.close())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10)


async def seed_pending_draft(pool: asyncpg.Pool, org_id: uuid.UUID) -> SeededDraft:
    """One tenant, mailbox, thread (with summary), inbound email, DRAFTED job and draft."""
    mbx_id, thread_id, msg_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organization (id, name) VALUES ($1, $2)", org_id, f"org-{org_id.hex[:6]}"
        )
        await conn.execute(
            "INSERT INTO mailbox (id, organization_id, provider, address, display_name, status)"
            " VALUES ($1, $2, 'gmail', $3, 'Support', 'active')",
            mbx_id,
            org_id,
            f"support-{mbx_id.hex[:6]}@example.com",
        )
        await conn.execute(
            "INSERT INTO email_thread (id, organization_id, mailbox_id, provider_thread_id)"
            " VALUES ($1, $2, $3, $4)",
            thread_id,
            org_id,
            mbx_id,
            f"th-{thread_id.hex[:6]}",
        )
        await conn.execute(
            "INSERT INTO thread_state (thread_id, organization_id, summary) VALUES ($1, $2, $3)",
            thread_id,
            org_id,
            THREAD_SUMMARY,
        )
        await conn.execute(
            """
            INSERT INTO email_message (
                id, organization_id, mailbox_id, thread_id, provider_message_id,
                direction, sender_email, sender_name, recipients, subject, body_text, received_at
            ) VALUES ($1, $2, $3, $4, $5, 'inbound', 'alice.smith@clientcorp.com',
                      'Alice Smith', '[]', 'Where is order ORD-82915?',
                      'Hello, where is my order ORD-82915? Thanks, Alice', now())
            """,
            msg_id,
            org_id,
            mbx_id,
            thread_id,
            f"prov-{msg_id.hex[:8]}",
        )
    job, _ = await PostgresJobStore(pool).create_job(
        Job(
            organization_id=org_id,
            message_id=msg_id,
            thread_id=thread_id,
            state=JobState.DRAFTED.value,
            idempotency_key=f"review-ui-{uuid.uuid4()}",
        )
    )
    draft = await PostgresDraftStore(pool).create_draft(
        GeneratedDraft(
            organization_id=org_id,
            job_id=job.id,
            message_id=msg_id,
            thread_id=thread_id,
            subject=SUBJECT,
            body=DRAFT_BODY,
            confidence=0.82,
            model_name="fake-model",
            model_tier="routine",
            prompt_version="billing.v2",
        )
    )
    return SeededDraft(draft_id=draft.id, job_id=uuid.UUID(str(job.id)), message_id=msg_id)


async def fetch_review_outcome(
    pool: asyncpg.Pool, org_id: uuid.UUID, draft_id: uuid.UUID
) -> ReviewOutcome:
    async with pool.acquire() as conn:
        draft = await conn.fetchrow(
            "SELECT status, body FROM generated_draft WHERE id = $1 AND organization_id = $2",
            draft_id,
            org_id,
        )
        rows = await conn.fetch(
            "SELECT decision, edited_body, edit_distance, review_ms FROM feedback"
            " WHERE draft_id = $1 AND organization_id = $2",
            draft_id,
            org_id,
        )
    assert draft is not None
    return ReviewOutcome(
        draft_status=draft["status"], draft_body=draft["body"], feedback=[dict(r) for r in rows]
    )


async def delete_organization(pool: asyncpg.Pool, org_id: uuid.UUID) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM organization WHERE id = $1", org_id)
