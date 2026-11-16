"""Pydantic schemas for the demo console's ingest door (ADR-0018, design.md §5.9, R25.1-R25.4)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

_EMAIL_PATTERN = r"^[^@\s<>]+@[^@\s<>]+$"


class DemoRunOptions(BaseModel):
    """How the pipeline runs a demo job; stored on the job and kept on retry and replay (R25.3)."""

    model_profile: Literal["fake", "live"] = Field(
        default="fake", description="`fake`: offline FakeLLMProvider; `live`: the LLM__* provider."
    )
    guard_mode: Literal["C0", "C3"] = Field(
        default="C0", description="`C0`: rag-email alone; `C3`: with AgentMailGuard (task 9.7)."
    )


class DemoThreadMessage(BaseModel):
    """An earlier message of the demo thread, oldest first."""

    sender_email: str = Field(pattern=_EMAIL_PATTERN, max_length=320)
    sender_name: str | None = Field(default=None, max_length=200)
    subject: str = Field(max_length=998)
    body: str = Field(max_length=100_000)


class DemoMessageRequest(BaseModel):
    """One demo email (with optional earlier thread messages) and its run options."""

    sender_email: str = Field(pattern=_EMAIL_PATTERN, max_length=320)
    sender_name: str | None = Field(default=None, max_length=200)
    subject: str = Field(max_length=998)
    body: str = Field(max_length=100_000)
    thread: list[DemoThreadMessage] = Field(
        default_factory=list,
        max_length=20,
        description="Earlier messages of the same thread, oldest first; ingested before this one.",
    )
    run_options: DemoRunOptions = Field(default_factory=DemoRunOptions)


class DemoMessageAccepted(BaseModel):
    """202 answer: the request is queued for the mail-connector."""

    demo_request_id: UUID = Field(description="Poll GET /v1/demo/requests/{id} with this.")
    status: Literal["accepted"] = "accepted"
    messages: int = Field(description="Emails queued: the thread messages plus this one.")
    run_options: DemoRunOptions


class DemoRequestStatus(BaseModel):
    """Where a demo request is: `pending` until the mail-connector created its job."""

    demo_request_id: UUID
    status: Literal["pending", "ingested"]
    job_id: UUID | None = Field(default=None, description="The job of the request's last email.")
    message_id: UUID | None = Field(
        default=None, description="Its normalized message, once the email-worker stored it."
    )
    job_state: str | None = None
    run_options: DemoRunOptions | None = None


class DemoCapabilities(BaseModel):
    """What the console can offer on this stack."""

    live_model_available: bool = Field(description="False: `model_profile=live` answers 422.")
    live_model: str | None = Field(default=None, description="The live fast-tier model name.")
    guard_available: bool = Field(description="AgentMailGuard in the ai-worker (task 9.7).")
    l1_classifier_loaded: bool = Field(description="The guard's private L1 classifier is mounted.")
    demo_mailbox_id: UUID | None = Field(
        default=None, description="This organization's demo mailbox; None until `make seed`."
    )
