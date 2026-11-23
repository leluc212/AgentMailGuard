"""L4 Output Guard (ADR-0015 R-e, R-f, plan_v3 design decision 1 / D1=A, F1).

One layer after the reply call: the output scanner checks the draft, then the shared
``PolicyEngine`` decides at stage ``outbound``, then the inbound decision is carried in as a
floor. There is no separate policy step after this layer.

The floor (enforced here, not configurable):

- it exists only when the inbound decision came from a specific rule (not a catch-all: the
  engine's ``default`` or a rule whose ``when`` is empty, read from the loaded policy) and its
  action is stricter than ``auto_send``;
- final action = strictest(outbound, floor); ``requires_human`` = OR; risk tier = the higher;
- the winner is the floor only when it is strictly stricter (a tie goes to the outbound
  decision); the winner names ``matched_rule_id``; ``matched_rule_ids`` is the union, inbound
  ids prefixed ``inbound:``;
- the auto-send allowlist is applied after the merge.

Invariants (tests/unit/test_output_guard_floor.py): with a floor, final rank >= floor rank;
``auto_send`` only without a floor and inside the allowlist; ``requires_human`` is the OR;
the ``audit_id`` is the same for two identical runs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from mailguard.config.settings import MailGuardSettings, get_settings
from mailguard.contracts.email import DraftCandidate, GuardedEmail, SourceDocument
from mailguard.contracts.policy import GuardReport, PolicyAction, PolicyDecision
from mailguard.contracts.verdict import InboundVerdict, LayerName, OutputVerdict
from mailguard.layers.output_guard.scanner import OutputScanner
from mailguard.llm.protocol import LLMProvider
from mailguard.policy.engine import PolicyEngine

LAYER = LayerName.OUTPUT_GUARD
INBOUND_PREFIX = "inbound:"
MAX_INJECTED_INSTRUCTIONS = 20


class OutputGuard:
    """L4 Output Guard: draft scan + outbound policy + inbound floor -> action and reason."""

    name = LAYER

    def __init__(
        self,
        settings: MailGuardSettings | None = None,
        *,
        scanner: OutputScanner | None = None,
        policy: PolicyEngine | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.scanner = scanner or OutputScanner(self.settings)
        self.policy = policy or PolicyEngine(self.settings)

    @property
    def llm(self) -> LLMProvider | None:
        """The output judge (the scanner's LLM stage), None when off."""
        return self.scanner.llm

    @llm.setter
    def llm(self, provider: LLMProvider | None) -> None:
        self.scanner.llm = provider

    # ------------------------------------------------------------------ API
    async def inspect(
        self,
        report: GuardReport,
        draft: DraftCandidate,
        *,
        email: GuardedEmail,
        intent: InboundVerdict | None,
        kept_chunks: Sequence[SourceDocument] = (),
        protected_texts: Sequence[str] = (),
        category: str | None = None,
    ) -> tuple[OutputVerdict, PolicyDecision]:
        """Scan ``draft``, then decide the outbound action with the inbound floor.

        ``report.output_guard`` is set before the policy reads it; ``report.inbound_decision``
        (None when the inbound gate is off) is the floor candidate. The merged decision is the
        one audited.
        """
        indicators, instructions = injected_context(report)
        verdict = await self.scanner.inspect(
            draft,
            email=email,
            intent=intent,
            allowed_citations={c.citation_id for c in kept_chunks}
            | {c.chunk_id for c in kept_chunks},
            protected_texts=protected_texts,
            trusted_texts=[c.content for c in kept_chunks],
            injected_indicators=indicators,
            injected_instructions=instructions,
        )
        report.output_guard = verdict
        outbound = self.policy.decide(
            report, stage="outbound", category=category, draft_action=draft.action, audit=False
        )
        decision = merge_floor(
            outbound,
            report.inbound_decision,
            catch_all=self.catch_all_rule_ids(),
            auto_send_categories=self.policy.auto_send_categories,
            category=category,
        )
        if self.policy.audit_enabled:
            self.policy.write_audit(decision, report)
        return verdict, decision

    def catch_all_rule_ids(self) -> frozenset[str]:
        """``default`` plus every loaded rule whose ``when`` is empty (never hard-coded)."""
        return frozenset({"default"} | {r.id for r in self.policy.rules if not r.when})


def injected_context(report: GuardReport) -> tuple[dict[str, list[str]], list[str]]:
    """What the inbound layers flagged, for the draft's injected-goal and indicator checks.

    Indicators (emails, urls) and instructions come from the Inbound Guard's scan and extract
    sub-stages, plus the excerpts of findings in quarantined knowledge (a payload found in a
    quarantined chunk must not surface in the draft either).
    """
    indicators: dict[str, list[str]] = {"emails": [], "urls": []}
    instructions: list[str] = []
    inbound = report.inbound_guard
    scan = (inbound.stage("scan") if inbound is not None else None) or {}
    extract = (inbound.stage("extract") if inbound is not None else None) or {}
    if scan:
        scan_meta = scan.get("metadata") or {}
        ind = scan_meta.get("indicators") or {}
        indicators = {"emails": list(ind.get("emails", [])), "urls": list(ind.get("urls", []))}
        instructions += list(scan_meta.get("injected_instructions", []))
    if inbound is not None:
        for f in inbound.findings:
            if f.metadata.get("stage") == "scan":
                instructions += list(f.metadata.get("injected_instructions", []))
    if extract and inbound is not None:
        instructions += [s.text for s in inbound.stripped_segments]
        extract_meta = extract.get("metadata") or {}
        instructions += list(extract_meta.get("instructions_to_assistant", []))
    if report.doc_scanner is not None:
        for cv in report.doc_scanner.chunks:
            if cv.quarantined:
                instructions += [f.excerpt for f in cv.findings if f.excerpt]
    unique = list(dict.fromkeys(i for i in instructions if i))
    return indicators, unique[:MAX_INJECTED_INSTRUCTIONS]


def floor_of(inbound: PolicyDecision | None, catch_all: frozenset[str]) -> PolicyDecision | None:
    """The inbound decision when it is a floor (specific rule, stricter than auto_send; F1)."""
    if inbound is None or inbound.matched_rule_id in catch_all:
        return None
    return inbound if inbound.action.rank > PolicyAction.AUTO_SEND.rank else None


def merge_floor(
    outbound: PolicyDecision,
    inbound: PolicyDecision | None,
    *,
    catch_all: frozenset[str],
    auto_send_categories: set[str],
    category: str | None,
) -> PolicyDecision:
    """The final decision: ``outbound`` merged with the inbound floor (design decision 1)."""
    floor = floor_of(inbound, catch_all)
    metadata = {**outbound.metadata, "decided_by": str(LAYER), "carried_from_inbound": False}
    if floor is None:
        decision = outbound.model_copy(update={"metadata": metadata}, deep=True)
        return _allowlist(decision, auto_send_categories, category)
    carried = floor.action.rank > outbound.action.rank  # a tie goes to the outbound decision
    winner = floor if carried else outbound
    reasons = list(outbound.reasons)
    if carried:
        carry = f"carried from inbound_gate: {floor.matched_rule_id} ({floor.action})"
        reasons = [carry, *reasons]
        metadata["decided_by"] = str(LayerName.INBOUND_GATE)
        metadata["carried_from_inbound"] = True
    metadata["inbound_floor"] = {"rule": floor.matched_rule_id, "action": str(floor.action)}
    rule_ids = list(outbound.matched_rule_ids)
    rule_ids += [
        f"{INBOUND_PREFIX}{r}" for r in (floor.matched_rule_ids or [floor.matched_rule_id])
    ]
    risk = max(outbound.risk_tier, floor.risk_tier, key=lambda t: t.rank)
    seed = f"{outbound.audit_id}|floor={floor.audit_id}|carried={carried}"
    decision = outbound.model_copy(
        update={
            "audit_id": hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32],
            "action": PolicyAction.strictest(outbound.action, floor.action),
            "risk_tier": risk,
            "matched_rule_id": winner.matched_rule_id,
            "matched_rule_ids": list(dict.fromkeys(rule_ids)),
            "reasons": reasons[:12],
            "requires_human": outbound.requires_human or floor.requires_human,
            "metadata": metadata,
        },
        deep=True,
    )
    return _allowlist(decision, auto_send_categories, category)


def _allowlist(
    decision: PolicyDecision, auto_send_categories: set[str], category: str | None
) -> PolicyDecision:
    """Never auto-send outside the allow-listed categories (applied after the merge)."""
    if decision.action is PolicyAction.AUTO_SEND and (category or "") not in auto_send_categories:
        reasons = [*decision.reasons]
        reasons[0:1] = [f"{reasons[0] if reasons else ''} (auto-send not allowed for category)"]
        return decision.model_copy(update={"action": PolicyAction.DRAFT_ONLY, "reasons": reasons})
    return decision


__all__ = ["OutputGuard", "floor_of", "injected_context", "merge_floor"]
