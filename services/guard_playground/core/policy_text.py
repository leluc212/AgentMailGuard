"""Rule reasons from the guard's ``configs/policy.yaml`` (design.md, ``policy_text.py``).

Requirement 5 criteria 3 and 7: the Outcome_Banner shows the full ``reason`` of the
matched rule, or "rule reason not found" when no rule with that id exists. The file is
read once at construction; this module never imports ``mailguard``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REASON_NOT_FOUND = "rule reason not found"
POLICY_RELATIVE_PATH = Path("configs") / "policy.yaml"


class PolicyText:
    """Lookup of rule ``reason`` text by rule id, loaded once from a policy YAML file.

    ``error`` is ``None`` when the file loaded; otherwise it names the file and the cause,
    and every lookup returns :data:`REASON_NOT_FOUND`.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.version: str | None = None
        self.error: str | None = None
        self._reasons: dict[str, str] = {}
        # short prefix ("P01") -> full ids sharing that prefix; used only when unambiguous.
        self._by_prefix: dict[str, list[str]] = {}
        self._load()

    @classmethod
    def from_guard_root(cls, guard_root: Path) -> PolicyText:
        return cls(Path(guard_root) / POLICY_RELATIVE_PATH)

    def _load(self) -> None:
        try:
            data: Any = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            self.error = f"cannot read policy file {self.path}: {exc}"
            return
        if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
            self.error = f"policy file {self.path} has no 'rules' list"
            return
        version = data.get("version")
        self.version = None if version is None else str(version)
        for rule in data["rules"]:
            if not isinstance(rule, dict) or not rule.get("id"):
                continue
            rule_id = str(rule["id"])
            reason = rule.get("reason")
            self._reasons[rule_id] = "" if reason is None else str(reason)
            self._by_prefix.setdefault(rule_id.split("-", 1)[0], []).append(rule_id)

    def reason(self, rule_id: str | None) -> str:
        """Return the rule's reason, or "rule reason not found"."""
        if not rule_id:
            return REASON_NOT_FOUND
        found = self._reasons.get(rule_id)
        if found is None:
            candidates = self._by_prefix.get(rule_id, [])
            if len(candidates) == 1:
                found = self._reasons[candidates[0]]
        return found if found else REASON_NOT_FOUND
