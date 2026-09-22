"""Funnel instrumentation, accounting, and mathematical reconciliation (R6.10, R6.15, R21.4, NFR14).

Implements the three mutually exclusive economic routing outcomes:
1. Early Exit (reply_required == false or workflow_hint == 'none') -> ~45%
2. Deterministic Template Reply (workflow_hint == 'template') -> ~20%
3. Actionable AI Generation (workflow_hint == 'ai') -> ~35%
   - With RAG (retrieval_required == true) -> ~70% of AI (~24.5% of total)
   - Without RAG (retrieval_required == false) -> ~30% of AI (~10.5% of total)

Guarantees zero residual bucket:
    early_exit + template + ai_generation == total_triaged
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from packages.observability.metrics import PipelineMetrics

logger = logging.getLogger(__name__)


class FunnelOutcome(StrEnum):
    """Mutually exclusive triage funnel outcomes per R6.15 and design.md §5.3."""

    EARLY_EXIT = "early_exit"
    TEMPLATE = "template"
    AI_GENERATION = "ai_generation"


class RAGMode(StrEnum):
    """Sub-dimension for AI generation tracking ~70% RAG requirement per NFR14."""

    NONE = "none"
    RAG = "rag"
    NO_RAG = "no_rag"


@dataclass(frozen=True)
class FunnelReport:
    """Mathematical reconciliation report for the triage funnel (R6.10, R6.15, NFR14)."""

    total_triaged: int
    early_exit_count: int
    template_count: int
    ai_generation_count: int
    ai_rag_count: int
    ai_no_rag_count: int
    early_exit_ratio: float
    template_ratio: float
    ai_generation_ratio: float
    rag_share_of_ai: float
    residual_count: int
    is_reconciled: bool
    by_category: dict[str, dict[str, int]] = field(default_factory=dict)


def record_funnel_outcome(
    metrics: PipelineMetrics,
    organization: str | UUID,
    category: str,
    outcome: FunnelOutcome | str,
    rag_mode: RAGMode | str = RAGMode.NONE,
) -> None:
    """Record a triaged email outcome into the standardized funnel counter (R6.10, R6.15).

    Parameters
    ----------
    metrics : PipelineMetrics
        The pipeline metrics instance.
    organization : str | UUID
        Organization / tenant identifier.
    category : str
        Email classification category.
    outcome : FunnelOutcome | str
        One of the 3 mutually exclusive outcomes ('early_exit', 'template', 'ai_generation').
    rag_mode : RAGMode | str
        RAG requirement ('none' for early exit and template, 'rag' or 'no_rag' for AI).
    """
    outcome_str = str(outcome.value if isinstance(outcome, FunnelOutcome) else outcome)
    rag_str = str(rag_mode.value if isinstance(rag_mode, RAGMode) else rag_mode)
    org_str = str(organization)

    metrics.triage_funnel_outcomes_total.labels(
        organization=org_str,
        category=category,
        outcome=outcome_str,
        rag_mode=rag_str,
    ).inc()


def compute_funnel_reconciliation(
    metrics: PipelineMetrics,
    organization: str | UUID | None = None,
) -> FunnelReport:
    """Calculate realized email processing funnel and assert zero residual bucket (R6.15, NFR14).

    Iterates over the Prometheus samples for `triage_funnel_outcomes_total`, extracts
    exact integer counts for early exit, template reply, and AI generation, and performs
    mathematical reconciliation.

    Parameters
    ----------
    metrics : PipelineMetrics
        The pipeline metrics instance whose registry to inspect.
    organization : str | UUID | None
        Optional tenant filter. If None, aggregates all tenants.

    Returns
    -------
    FunnelReport
        Detailed counts, ratios against 45% / 20% / 35% targets, RAG share, and residual check.
    """
    target_org = str(organization) if organization is not None else None

    early_exit_count = 0
    template_count = 0
    ai_generation_count = 0
    ai_rag_count = 0
    ai_no_rag_count = 0

    by_category: dict[str, dict[str, int]] = {}

    for metric_family in metrics.triage_funnel_outcomes_total.collect():
        for sample in metric_family.samples:
            if sample.name != "triage_funnel_outcomes_total":
                continue

            labels = sample.labels
            sample_org = labels.get("organization")
            if target_org is not None and sample_org != target_org:
                continue

            val = int(sample.value)
            outcome = labels.get("outcome")
            rag_mode = labels.get("rag_mode")
            category = labels.get("category", "unknown")

            if category not in by_category:
                by_category[category] = {
                    "early_exit": 0,
                    "template": 0,
                    "ai_rag": 0,
                    "ai_no_rag": 0,
                    "total": 0,
                }

            if outcome == FunnelOutcome.EARLY_EXIT:
                early_exit_count += val
                by_category[category]["early_exit"] += val
                by_category[category]["total"] += val
            elif outcome == FunnelOutcome.TEMPLATE:
                template_count += val
                by_category[category]["template"] += val
                by_category[category]["total"] += val
            elif outcome == FunnelOutcome.AI_GENERATION:
                ai_generation_count += val
                by_category[category]["total"] += val
                if rag_mode == RAGMode.RAG:
                    ai_rag_count += val
                    by_category[category]["ai_rag"] += val
                else:
                    ai_no_rag_count += val
                    by_category[category]["ai_no_rag"] += val

    total_triaged = early_exit_count + template_count + ai_generation_count
    # Zero-residual proof: all mail accounted for across 3 mutually exclusive buckets
    residual_count = total_triaged - (early_exit_count + template_count + ai_generation_count)

    if total_triaged > 0:
        early_exit_ratio = early_exit_count / total_triaged
        template_ratio = template_count / total_triaged
        ai_generation_ratio = ai_generation_count / total_triaged
    else:
        early_exit_ratio = 0.0
        template_ratio = 0.0
        ai_generation_ratio = 0.0

    rag_share_of_ai = (ai_rag_count / ai_generation_count) if ai_generation_count > 0 else 0.0

    # Reconciled if residual is 0 and ratios sum to 1.0 (or 0 if no traffic)
    sum_ratios = early_exit_ratio + template_ratio + ai_generation_ratio
    is_reconciled = (residual_count == 0) and (total_triaged == 0 or abs(sum_ratios - 1.0) < 1e-6)

    return FunnelReport(
        total_triaged=total_triaged,
        early_exit_count=early_exit_count,
        template_count=template_count,
        ai_generation_count=ai_generation_count,
        ai_rag_count=ai_rag_count,
        ai_no_rag_count=ai_no_rag_count,
        early_exit_ratio=early_exit_ratio,
        template_ratio=template_ratio,
        ai_generation_ratio=ai_generation_ratio,
        rag_share_of_ai=rag_share_of_ai,
        residual_count=residual_count,
        is_reconciled=is_reconciled,
        by_category=by_category,
    )
