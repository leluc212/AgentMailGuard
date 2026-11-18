"""Demo scenario catalog read from configuration (task 9.5, R25.2, R25.8, ADR-0018 decision 5).

``config/demo_scenarios.yaml`` holds S01-S14 and G01-G06 with their emails, run options and
expected outcomes. The catalog is validated when it is loaded, so a broken file fails the API's
start-up instead of a run during the defence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from services.api.schemas.demo import DemoRunOptions, DemoThreadMessage


class LocalizedText(BaseModel):
    """A text in Vietnamese and English (the console's two languages)."""

    vi: str
    en: str


class ScenarioEmail(DemoThreadMessage):
    """The scenario's own email (the last of its thread)."""


class ScenarioKnowledge(BaseModel):
    """A document the console uploads and waits to be active before the run."""

    title: str
    category: str
    content: str


class ScenarioSource(BaseModel):
    """Where a copied case comes from (ADR-0018 decision 5)."""

    case_id: str
    dataset: str
    origin: str
    license: str
    modified: str | None = None


class ScenarioRunOptions(BaseModel):
    """Run options of a scenario; a comparison names no guard mode (it runs both)."""

    model_profile: Literal["fake", "live"] = "fake"
    guard_mode: Literal["C0", "C3"] | None = None


Kind = Literal["email", "comparison", "flow"]
Flow = Literal["cross_tenant", "replay", "review", "knowledge", "burst", "search_debug"]


class Scenario(BaseModel):
    """One catalog entry."""

    id: str = Field(pattern=r"^[SG]\d{2}$")
    group: Literal["pipeline", "guard"]
    kind: Kind
    flow: Flow | None = None
    title: LocalizedText
    description: LocalizedText
    run_options: ScenarioRunOptions = Field(default_factory=ScenarioRunOptions)
    email: ScenarioEmail | None = None
    thread: list[DemoThreadMessage] = Field(default_factory=list)
    knowledge: list[ScenarioKnowledge] = Field(default_factory=list)
    burst: int | None = Field(default=None, ge=1, le=200)
    query: str | None = None
    source: ScenarioSource | None = None
    requires_live: bool = Field(
        default=False, description="Runs only with the live model (G06, owner decision C13)."
    )
    expected: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> Scenario:
        if self.kind == "flow" and self.flow is None:
            raise ValueError(f"{self.id}: a flow scenario names its flow")
        if self.kind != "flow" and self.flow is not None:
            raise ValueError(f"{self.id}: only a flow scenario names a flow")
        if self.flow == "search_debug":
            if not self.query:
                raise ValueError(f"{self.id}: search_debug needs a query")
        elif self.email is None:
            raise ValueError(f"{self.id}: needs an email")
        if self.flow == "burst" and not self.burst:
            raise ValueError(f"{self.id}: a burst names its size")
        if self.kind == "comparison" and self.run_options.guard_mode is not None:
            raise ValueError(f"{self.id}: a comparison runs both guard modes; name none")
        if self.requires_live and self.run_options.model_profile != "live":
            raise ValueError(f"{self.id}: requires_live needs model_profile live")
        return self

    @property
    def runnable(self) -> bool:
        """Whether ``POST /v1/demo/scenarios/{id}/run`` sends emails for it."""
        return self.email is not None

    def runs(self) -> list[DemoRunOptions]:
        """The run options of each email the scenario sends, in order (R25.8)."""
        profile = self.run_options.model_profile
        if self.kind == "comparison":
            return [
                DemoRunOptions(model_profile=profile, guard_mode="C0"),
                DemoRunOptions(model_profile=profile, guard_mode="C3"),
            ]
        guard_mode = self.run_options.guard_mode or "C0"
        options = DemoRunOptions(model_profile=profile, guard_mode=guard_mode)
        return [options] * (self.burst or 1)


class ScenarioCatalog(BaseModel):
    """The whole file."""

    version: int
    scenarios: list[Scenario]

    @model_validator(mode="after")
    def _unique_ids(self) -> ScenarioCatalog:
        ids = [s.id for s in self.scenarios]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate scenario ids: {duplicates}")
        return self

    def get(self, scenario_id: str) -> Scenario | None:
        return next((s for s in self.scenarios if s.id == scenario_id), None)


def load_catalog(path: str | Path) -> ScenarioCatalog:
    """Read and validate the catalog.

    Raises:
        FileNotFoundError: If the file is missing.
        pydantic.ValidationError: If an entry is malformed.
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return ScenarioCatalog.model_validate(data)
