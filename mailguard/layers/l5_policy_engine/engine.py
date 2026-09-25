"""Layer 5 - Email Policy Engine (risk-tiered action gating).

Rules are declared in ``configs/policy.yaml`` and evaluated in priority order over
*facts* derived from the ``GuardReport``. The engine is deterministic and
idempotent: the same report and stage always produce the same ``audit_id``, so a
replayed job never creates a second decision. Every decision is appended to a
JSONL audit log (no email content, only ids, scores, rule ids and hashes).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from mailguard.config.settings import MailGuardSettings, get_settings
from mailguard.contracts.policy import GuardReport, PolicyAction, PolicyDecision, RiskTier
from mailguard.contracts.verdict import LayerName, Severity, ThreatType

logger = logging.getLogger(__name__)

LAYER = LayerName.L5_POLICY_ENGINE

_SEVERITIES = list(Severity)


def _severity(value: str | Severity) -> Severity:
    return value if isinstance(value, Severity) else Severity(str(value).lower())


class PolicyRule:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.id = str(raw["id"])
        self.priority = int(raw.get("priority", 100))
        self.when: dict[str, Any] = dict(raw.get("when") or {})
        self.action = PolicyAction(str(raw.get("action", "draft_only")))
        self.requires_human = bool(raw.get("requires_human", self.action.rank >= 2))
        self.reason = str(raw.get("reason", ""))

    def matches(self, facts: dict[str, Any]) -> bool:
        w = self.when
        if "stage" in w and facts["stage"] != w["stage"]:
            return False
        if (
            "max_severity_at_least" in w
            and facts["max_severity"].rank < _severity(w["max_severity_at_least"]).rank
        ):
            return False
        if (
            "max_severity_at_most" in w
            and facts["max_severity"].rank > _severity(w["max_severity_at_most"]).rank
        ):
            return False
        if "threat_types_any" in w:
            wanted = {str(t) for t in w["threat_types_any"]}
            if not (wanted & facts["threat_types"]):
                return False
        if "layers_flagged_any" in w:
            wanted = {str(x) for x in w["layers_flagged_any"]}
            if not (wanted & facts["layers_flagged"]):
                return False
        if w.get("layer_error_any") and not facts["layer_error"]:
            return False
        if "quarantine_ratio_at_least" in w and facts["quarantine_ratio"] < float(
            w["quarantine_ratio_at_least"]
        ):
            return False
        if "removed_ratio_at_least" in w and facts["removed_ratio"] < float(
            w["removed_ratio_at_least"]
        ):
            return False
        if w.get("complied_with_injected_goal") and not facts["complied_with_injected_goal"]:
            return False
        if w.get("citation_mismatch") and not facts["citation_mismatch"]:
            return False
        if "redactions_at_least" in w and facts["redactions"] < int(w["redactions_at_least"]):
            return False
        if "category_in" in w and (facts["category"] or "") not in {
            str(c) for c in w["category_in"]
        }:
            return False
        allowed_actions = {str(a) for a in w.get("action_in", [])}
        return not ("action_in" in w and (facts["draft_action"] or "") not in allowed_actions)


class PolicyEngine:
    name = LAYER

    def __init__(
        self,
        settings: MailGuardSettings | None = None,
        *,
        policy_path: Path | None = None,
        audit_path: Path | None = None,
        audit: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.cfg = self.settings.l5
        self.policy_path = policy_path or self.settings.resolve(self.cfg.policy_path)
        self.audit_path = audit_path or self.settings.resolve(self.cfg.audit_log_path)
        self.audit_enabled = audit
        self.version = "unknown"
        self.default_action = PolicyAction.DRAFT_ONLY
        self.auto_send_categories: set[str] = set()
        self.rules: list[PolicyRule] = []
        self.reload()

    def reload(self) -> None:
        if not self.policy_path.exists():
            logger.warning(
                "policy not found at %s; using fail-safe default (draft_only)", self.policy_path
            )
            self.rules = []
            return
        with open(self.policy_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        self.version = str(data.get("version", "unknown"))
        self.default_action = PolicyAction(str(data.get("default_action", "draft_only")))
        self.auto_send_categories = {str(c) for c in data.get("auto_send_categories", [])}
        rules = []
        for raw in data.get("rules", []):
            try:
                rules.append(PolicyRule(raw))
            except (KeyError, ValueError) as exc:
                logger.error("skipping invalid policy rule %s: %s", raw.get("id"), exc)
        self.rules = sorted(rules, key=lambda r: r.priority)

    # ------------------------------------------------------------------ facts
    @staticmethod
    def facts(
        report: GuardReport, *, stage: str, category: str | None, draft_action: str | None
    ) -> dict[str, Any]:
        verdicts = report.verdicts()
        threat_types: set[str] = set()
        layers_flagged: set[str] = set()
        for v in verdicts:
            for f in v.findings:
                threat_types.add(str(f.threat_type))
            if v.severity.rank >= Severity.MEDIUM.rank:
                layers_flagged.add(str(v.layer))
        quarantine_ratio = (
            sum(1 for c in report.l3b if c.quarantined) / len(report.l3b) if report.l3b else 0.0
        )
        return {
            "stage": stage,
            "max_severity": report.max_severity,
            "threat_types": threat_types,
            "layers_flagged": layers_flagged,
            "layer_error": any(v.error for v in verdicts),
            "quarantine_ratio": quarantine_ratio,
            "removed_ratio": report.l2.removed_ratio if report.l2 is not None else 0.0,
            "complied_with_injected_goal": bool(
                report.l4 and report.l4.complied_with_injected_goal
            ),
            "citation_mismatch": bool(report.l4 and report.l4.citation_mismatch),
            "redactions": len(report.l4.redactions) if report.l4 else 0,
            "category": category,
            "draft_action": draft_action,
        }

    # ------------------------------------------------------------------ decide
    def decide(
        self,
        report: GuardReport,
        *,
        stage: str = "outbound",
        category: str | None = None,
        draft_action: str | None = None,
    ) -> PolicyDecision:
        facts = self.facts(report, stage=stage, category=category, draft_action=draft_action)
        matched: PolicyRule | None = next((r for r in self.rules if r.matches(facts)), None)
        if matched is None:
            action, rule_id, reason, requires_human = (
                self.default_action,
                "default",
                "no rule matched; default posture",
                self.default_action.rank >= 2,
            )
        else:
            action, rule_id, reason, requires_human = (
                matched.action,
                matched.id,
                matched.reason,
                matched.requires_human,
            )
        # never auto-send outside the allow-listed categories, even if a rule says so
        if action is PolicyAction.AUTO_SEND and (category or "") not in self.auto_send_categories:
            action, reason = (
                PolicyAction.DRAFT_ONLY,
                reason + " (auto-send not allowed for category)",
            )
        risk = RiskTier.from_severity(facts["max_severity"])
        if facts["quarantine_ratio"] >= 0.6 and risk.rank < RiskTier.T3_HIGH.rank:
            risk = RiskTier.T3_HIGH
        reasons = [reason]
        reasons += [
            f"{v.layer}: {v.severity} (score={v.score:.2f}, by={v.decided_by})"
            for v in report.verdicts()
            if v.severity.rank >= Severity.MEDIUM.rank or v.error
        ]
        seed = "|".join(
            [
                report.message_id or "",
                report.organization_id or "",
                stage,
                self.version,
                str(action),
                rule_id,
                str(facts["max_severity"]),
            ]
        )
        decision = PolicyDecision(
            audit_id=hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32],
            action=action,
            risk_tier=risk,
            matched_rule_id=rule_id,
            reasons=reasons[:12],
            requires_human=requires_human,
            redactions_applied=facts["redactions"],
            quarantined_chunk_ids=[c.chunk_id for c in report.l3b if c.quarantined],
            policy_version=self.version,
            metadata={
                "stage": stage,
                "category": category,
                "draft_action": draft_action,
                "threat_types": sorted(facts["threat_types"]),
                "layers_flagged": sorted(facts["layers_flagged"]),
                "quarantine_ratio": round(facts["quarantine_ratio"], 3),
                "removed_ratio": round(facts["removed_ratio"], 3),
            },
        )
        if self.audit_enabled:
            self._audit(decision, report)
        return decision

    # ------------------------------------------------------------------ audit
    def _audit(self, decision: PolicyDecision, report: GuardReport) -> None:
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": datetime.now(UTC).isoformat(),
                "audit_id": decision.audit_id,
                "message_id": report.message_id,
                "organization_id": report.organization_id,
                "stage": decision.metadata.get("stage"),
                "action": str(decision.action),
                "risk_tier": str(decision.risk_tier),
                "rule": decision.matched_rule_id,
                "policy_version": decision.policy_version,
                "requires_human": decision.requires_human,
                "layers": {
                    str(v.layer): {
                        "severity": str(v.severity),
                        "score": v.score,
                        "by": v.decided_by,
                    }
                    for v in report.verdicts()
                    if v.layer is not LayerName.L3B_DOCUMENT_SCANNER
                },
                "l3b": {
                    "chunks": len(report.l3b),
                    "quarantined": decision.quarantined_chunk_ids,
                },
                "threat_types": decision.metadata.get("threat_types", []),
                "redactions": decision.redactions_applied,
            }
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # never fail a decision because the log is unavailable
            logger.error("audit log write failed: %s", exc)


__all__ = ["PolicyEngine", "PolicyRule", "ThreatType"]
