"""``RunResult`` — the Replay_Store schema v1 (design.md, ``core/schema.py``).

One closed schema shared by Live runs, Replay files, tests and batch generation (TD5).
Every model forbids extra fields so a Replay file with an unknown key fails validation and
is skipped (Requirement 7.12). The final decision is never stored; ``RunResult.final`` derives
it as ``outbound or inbound`` so the two can never disagree.

This module deliberately does not import ``mailguard``; building a ``RunResult`` from a guard
report lives elsewhere.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

PRESETS: tuple[str, ...] = (
    "C0",
    "C1",
    "C2",
    "C3",
    "C3-L1",
    "C3-L2",
    "C3-L3",
    "C3-L4",
    "C3-ISO",
)
ACTIONS: tuple[str, ...] = ("quarantine", "block", "human_approval", "draft_only", "auto_send")
LAYER_ORDER: tuple[str, ...] = ("inbound_guard", "doc_scanner", "output_guard")

LayerName = Literal["inbound_guard", "doc_scanner", "output_guard"]
Source = Literal["pinned", "fixture", "custom", "external"]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Finding(_Closed):
    detector: str
    rule_id: str | None
    score: float
    technique: str | None
    excerpt: str


class LayerRow(_Closed):
    layer: LayerName
    ran: bool
    not_run_reason: Literal["disabled by preset", "stopped by policy"] | None
    severity: str
    score: float
    decided_by: str
    latency_ms: int
    findings: list[Finding]
    error: str | None
    fallback_reason: str | None
    judge_configured: bool


class Stripped(_Closed):
    text: str
    reason: str
    score: float | None


class ChunkRow(_Closed):
    chunk_id: str
    content: str
    score: float | None
    quarantined: bool
    reason_rule_id: str | None
    reason_technique: str | None


class Decision(_Closed):
    action: str
    matched_rule_id: str
    risk_tier: str
    requires_human: bool

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        if value not in ACTIONS:
            raise ValueError(f"unknown action {value!r}; expected one of {ACTIONS}")
        return value


class Thresholds(_Closed):
    block: float | None
    flag: float | None
    strip: float | None
    quarantine: float | None


class ChunkInput(_Closed):
    chunk_id: str
    content: str


class EmailInput(_Closed):
    subject: str
    body: str
    chunks: list[ChunkInput]
    source: Source
    case_id: str | None


class RunResult(_Closed):
    schema_version: Literal[1] = 1
    generated_at: datetime  # UTC
    mode: Literal["live", "replay"]
    source: Source
    case_id: str | None
    preset: str
    draft_model: str  # DraftModel key
    guard_models: dict[str, str]  # judge -> model id (TD1)
    email: EmailInput
    layers: list[LayerRow]  # always 3 rows, pipeline order
    user_intent: str | None
    stripped: list[Stripped]
    removed_ratio: float
    chunks: list[ChunkRow]
    quarantine_ratio: float
    inbound: Decision | None
    outbound: Decision | None
    gate_passes: int | None  # report.metadata["gate_passes"]
    isolation_applied: bool
    draft_generated: bool
    draft_body: str | None  # draft after L4; None when blocked at the inbound gate
    guarded_prompt: str | None  # bundle.messages[1].content
    thresholds: Thresholds
    error: str | None = None

    @field_validator("preset")
    @classmethod
    def _known_preset(cls, value: str) -> str:
        if value not in PRESETS:
            raise ValueError(f"unknown preset {value!r}; expected one of {PRESETS}")
        return value

    @field_validator("generated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware (UTC)")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _three_layers_in_order(self) -> RunResult:
        names = tuple(row.layer for row in self.layers)
        if names != LAYER_ORDER:
            raise ValueError(f"layers must be exactly {LAYER_ORDER} in order, got {names}")
        return self

    @property
    def final(self) -> Decision | None:
        """Final decision: the outbound decision if L4 decided, else the inbound one."""
        return self.outbound or self.inbound
