"""View model of one result column (design.md "core/view_model.py"; Requirements 4, 5, 12, 14,
15).

``build_column`` turns a ``RunResult`` into plain dataclasses the Jinja2 templates render with
autoescape on. The only ``Markup`` values produced here are the highlighted email body and KB
chunks (built from escaped text by ``core/highlight.py``) and the fixed banner SVG icons below;
every other field is a plain ``str`` that Jinja2 escapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from markupsafe import Markup

from services.guard_playground.core.highlight import Needle, RefLocation, highlight_email
from services.guard_playground.core.policy_text import PolicyText
from services.guard_playground.core.schema import Decision, LayerRow, RunResult
from services.guard_playground.core.scores import (
    DASH,
    fmt_score,
    layer_score,
    pct1,
    quarantine_score,
    strip_score,
)

# --------------------------------------------------------------------------- banner

NO_DECISION = "none"
BANNER_TEXT: dict[str, str] = {
    "block": "BLOCKED",
    "quarantine": "QUARANTINED",
    "human_approval": "NEEDS HUMAN REVIEW",
    "draft_only": "DRAFT ONLY",
    "auto_send": "AUTO-SEND",
    NO_DECISION: "NO GUARD DECISION",
}
BANNER_CSS: dict[str, str] = {
    "block": "banner--block",
    "quarantine": "banner--quarantine",
    "human_approval": "banner--review",
    "draft_only": "banner--draft",
    "auto_send": "banner--send",
    NO_DECISION: "banner--none",
}

# One distinct shape per outcome (Requirement 12.4): octagon, filled square, diamond, circle,
# triangle, ring. Drawn in currentColor so the icon follows the banner text color.
_SHAPES: dict[str, str] = {
    "block": '<polygon points="7,1 17,1 23,7 23,17 17,23 7,23 1,17 1,7" fill="currentColor"/>',
    "quarantine": '<rect x="3" y="3" width="18" height="18" fill="currentColor"/>',
    "human_approval": '<polygon points="12,1 23,12 12,23 1,12" fill="currentColor"/>',
    "draft_only": '<circle cx="12" cy="12" r="10" fill="currentColor"/>',
    "auto_send": '<polygon points="12,2 23,22 1,22" fill="currentColor"/>',
    NO_DECISION: (
        '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="3"/>'
    ),
}
BANNER_ICON: dict[str, Markup] = {
    key: Markup(
        '<svg class="banner__icon" role="img" aria-label="{label}" viewBox="0 0 24 24" '
        'width="28" height="28" focusable="false">{shape}</svg>'
    ).format(label=BANNER_TEXT[key], shape=Markup(shape))
    for key, shape in _SHAPES.items()
}

# --------------------------------------------------------------------------- layers

LAYER_LABEL: dict[str, str] = {
    "inbound_guard": "L1 Inbound Guard",
    "doc_scanner": "L2 Doc Scanner",
    "output_guard": "L4 Output Guard",
}
LAYER_SHORT: dict[str, str] = {"inbound_guard": "L1", "doc_scanner": "L2", "output_guard": "L4"}
SEVERITY_RANK: dict[str, int] = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

NOT_RUN = "not run"
JUDGE_NOT_CONFIGURED = "judge not configured"
NO_FINDINGS = "no findings"
NO_DRAFT = "No draft was generated"
NO_LAYER_VERDICTS = "no layer verdicts"
C0_DISPLAY = "C0 (guard template, no layer)"


@dataclass(frozen=True)
class Banner:
    text: str
    icon_svg: Markup
    css: str
    rule_id: str | None
    reason: str | None
    score_line: str | None
    preset: str


@dataclass(frozen=True)
class FindingView:
    anchor_id: str
    detector: str
    rule_id: str  # "—" when missing
    score_text: str
    technique: str  # "—" when missing
    excerpt: str
    back_link: str | None  # id of the first Evidence_Highlight
    count: int
    note: str | None


@dataclass(frozen=True)
class StrippedView:
    anchor_id: str
    text: str
    reason: str
    score_text: str
    back_link: str | None
    count: int
    note: str | None


@dataclass(frozen=True)
class QuarantinedView:
    chunk_id: str
    rule_id: str
    technique: str


@dataclass
class LayerView:
    layer: str
    name: str
    anchor_id: str
    ran: bool
    severity: str
    severity_css: str
    score_text: str
    decided_by: str
    latency_ms: int
    findings: list[FindingView]
    no_findings: bool
    not_run_label: str | None
    not_run_reason: str | None
    judge_label: str | None
    error_label: str | None
    expanded: bool
    # L1 only
    user_intent: str | None = None
    stripped: list[StrippedView] = field(default_factory=list)
    removed_ratio_text: str | None = None
    # L2 only
    quarantined: list[QuarantinedView] = field(default_factory=list)
    quarantine_count: str | None = None
    quarantine_pct: str | None = None


@dataclass(frozen=True)
class ChunkView:
    chunk_id: str
    html: Markup
    quarantined: bool
    quarantine_reason: str | None
    score_text: str


@dataclass(frozen=True)
class DecisionView:
    label: str
    action: str
    matched_rule_id: str
    risk_tier: str
    requires_human: str  # "yes" / "no"
    reason: str


@dataclass(frozen=True)
class DraftView:
    generated: bool
    label: str
    body: str | None
    statement: str | None
    stop_action: str | None
    stop_rule_id: str | None


@dataclass
class ColumnView:
    banner: Banner
    stepper: list[Any]
    layers: list[LayerView]
    highlighted_body: Markup
    highlighted_chunks: list[ChunkView]
    draft: DraftView
    label: str
    preset: str
    preset_display: str
    mode: str
    inbound: DecisionView | None
    final: DecisionView | None
    deciding_layer: str | None
    no_layer_verdicts: bool
    guarded_prompt: str | None = None  # shown in its own collapsed section when present


# --------------------------------------------------------------------------- helpers


def _rank(severity: str) -> int:
    return SEVERITY_RANK.get(severity.lower(), 0)


def deciding_layer(r: RunResult) -> LayerRow | None:
    """Highest non-``none`` severity among rows that ran; ties go to the later layer."""
    best: LayerRow | None = None
    for row in r.layers:
        if not row.ran or _rank(row.severity) == 0:
            continue
        if best is None or _rank(row.severity) >= _rank(best.severity):
            best = row
    return best


def banner_key(r: RunResult) -> str:
    final = r.final
    return NO_DECISION if final is None else final.action


def _banner(r: RunResult, policy: PolicyText, decider: LayerRow | None) -> Banner:
    key = banner_key(r)
    final = r.final
    score_line = None
    if final is not None and decider is not None and decider.layer != "output_guard":
        score_line = layer_score(decider.layer, decider.score, r.thresholds)
    return Banner(
        text=BANNER_TEXT[key],
        icon_svg=BANNER_ICON[key],
        css=BANNER_CSS[key],
        rule_id=None if final is None else final.matched_rule_id,
        reason=None if final is None else policy.reason(final.matched_rule_id),
        score_line=score_line,
        preset=r.preset,
    )


def _decision(label: str, d: Decision | None, policy: PolicyText) -> DecisionView | None:
    if d is None:
        return None
    return DecisionView(
        label=label,
        action=d.action,
        matched_rule_id=d.matched_rule_id,
        risk_tier=d.risk_tier,
        requires_human="yes" if d.requires_human else "no",
        reason=policy.reason(d.matched_rule_id),
    )


def _finding_anchor(prefix: str, layer: str, i: int) -> str:
    return f"f-{prefix}-{layer}-{i}"


def _strip_anchor(prefix: str, i: int) -> str:
    return f"f-{prefix}-strip-{i}"


def _ref_id(anchor: str) -> str:
    """Needle ref id for an anchor; ``render`` links to ``#f-{ref_id}``."""
    return anchor.removeprefix("f-")


def _needles(r: RunResult, prefix: str) -> list[Needle]:
    """Finding excerpts of every layer (pipeline order) followed by stripped segments."""
    needles: list[Needle] = []
    for row in r.layers:
        for i, f in enumerate(row.findings):
            needles.append(
                Needle(
                    f.excerpt,
                    _ref_id(_finding_anchor(prefix, row.layer, i)),
                    f"{LAYER_SHORT[row.layer]} · {f.rule_id or DASH}",
                )
            )
    for i, s in enumerate(r.stripped):
        needles.append(Needle(s.text, _ref_id(_strip_anchor(prefix, i)), "L1 · strip"))
    return needles


def _label(r: RunResult) -> str:
    label = f"{r.preset} · {'REPLAY' if r.mode == 'replay' else 'LIVE'} · {r.draft_model}"
    if r.mode == "replay":
        label += f" · generated {r.generated_at:%Y-%m-%d %H:%M}"
    return label


def _draft(r: RunResult, compare_with_c0: bool) -> DraftView:
    if r.draft_generated:
        label = (
            "Unguarded_Draft (C0)"
            if r.preset == "C0" and compare_with_c0
            else f"Draft ({r.preset})"
        )
        return DraftView(True, label, r.draft_body or "", None, None, None)
    final = r.final
    return DraftView(
        generated=False,
        label=f"Draft ({r.preset})",
        body=None,
        statement=NO_DRAFT,
        stop_action=None if final is None else final.action,
        stop_rule_id=None if final is None else final.matched_rule_id,
    )


def _stepper(r: RunResult) -> list[Any]:
    try:
        from services.guard_playground.core.stepper import stages
    except ImportError:  # stepper module not available yet
        return []
    return list(stages(r))


def _layer_view(
    r: RunResult,
    row: LayerRow,
    prefix: str,
    locs: dict[str, RefLocation],
    expanded: bool,
) -> LayerView:
    findings = []
    for i, f in enumerate(row.findings):
        anchor = _finding_anchor(prefix, row.layer, i)
        loc = locs[anchor]
        findings.append(
            FindingView(
                anchor_id=anchor,
                detector=f.detector,
                rule_id=f.rule_id or DASH,
                score_text=fmt_score(f.score),
                technique=f.technique or DASH,
                excerpt=f.excerpt,
                back_link=loc.first_id,
                count=loc.count,
                note=loc.note,
            )
        )
    severity = row.severity
    view = LayerView(
        layer=row.layer,
        name=LAYER_LABEL[row.layer],
        anchor_id=f"l-{prefix}-{row.layer}",
        ran=row.ran,
        severity=severity,
        severity_css=f"sev--{severity.lower() if severity.lower() in SEVERITY_RANK else 'unknown'}",
        score_text=layer_score(row.layer, row.score, r.thresholds),
        decided_by=row.decided_by,
        latency_ms=int(row.latency_ms),
        findings=findings,
        no_findings=not row.findings,
        not_run_label=None if row.ran else NOT_RUN,
        not_run_reason=None if row.ran else row.not_run_reason,
        judge_label=JUDGE_NOT_CONFIGURED if row.ran and not row.judge_configured else None,
        error_label=row.error or row.fallback_reason or None,
        expanded=expanded,
    )
    if row.layer == "inbound_guard":
        view.user_intent = r.user_intent
        for i, s in enumerate(r.stripped):
            anchor = _strip_anchor(prefix, i)
            loc = locs[anchor]
            view.stripped.append(
                StrippedView(
                    anchor_id=anchor,
                    text=s.text,
                    reason=s.reason,
                    score_text=strip_score(s.score, r.thresholds),
                    back_link=loc.first_id,
                    count=loc.count,
                    note=loc.note,
                )
            )
        view.removed_ratio_text = pct1(r.removed_ratio)
    elif row.layer == "doc_scanner":
        view.quarantined = [
            QuarantinedView(c.chunk_id, c.reason_rule_id or DASH, c.reason_technique or DASH)
            for c in r.chunks
            if c.quarantined
        ]
        total = len(r.chunks)
        q = len(view.quarantined)
        view.quarantine_count = f"{q}/{total}"
        view.quarantine_pct = pct1(q / total) if total else pct1(r.quarantine_ratio)
    return view


# --------------------------------------------------------------------------- entry point


def build_column(
    r: RunResult, policy: PolicyText, *, id_prefix: str, compare_with_c0: bool = False
) -> ColumnView:
    """Build everything one result column shows. ``id_prefix`` keeps element ids unique per
    column (e.g. ``"a"`` and ``"b"`` in comparison mode)."""
    decider = deciding_layer(r)
    needles = _needles(r, id_prefix)
    texts = highlight_email(
        r.email.body, [c.content for c in r.chunks], needles, id_prefix=id_prefix
    )
    locs = {f"f-{loc.needle.ref_id}": loc for loc in texts.locations}  # keyed by anchor id

    layers = [
        _layer_view(r, row, id_prefix, locs, expanded=decider is not None and row is decider)
        for row in r.layers
    ]
    chunks = [
        ChunkView(
            chunk_id=c.chunk_id,
            html=html,
            quarantined=c.quarantined,
            quarantine_reason=(
                f"{c.reason_rule_id or DASH} · {c.reason_technique or DASH}"
                if c.quarantined
                else None
            ),
            score_text=DASH if c.score is None else quarantine_score(c.score, r.thresholds),
        )
        for c, html in zip(r.chunks, texts.chunks, strict=True)
    ]
    return ColumnView(
        banner=_banner(r, policy, decider),
        stepper=_stepper(r),
        layers=layers,
        highlighted_body=texts.body,
        highlighted_chunks=chunks,
        draft=_draft(r, compare_with_c0),
        label=_label(r),
        preset=r.preset,
        preset_display=C0_DISPLAY if r.preset == "C0" else r.preset,
        mode=r.mode,
        inbound=_decision("inbound", r.inbound, policy),
        final=_decision("final decision", r.final, policy),
        deciding_layer=None if decider is None else decider.layer,
        no_layer_verdicts=not any(row.ran for row in r.layers),
        guarded_prompt=r.guarded_prompt or None,
    )
