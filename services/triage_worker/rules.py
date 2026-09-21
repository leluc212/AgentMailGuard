"""Hot-reloadable YAML rule engine for the cascading triage worker (R6.1, R6.8).

Wraps the pure domain RuleEngine, providing automatic mtime-based hot-reloading
from declarative YAML/JSON configuration files with fail-safe error isolation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from packages.domain.entities import Classification, NormalizedMessage
from packages.domain.rules import EmailContext, Rule, RuleEngine

logger = logging.getLogger(__name__)


def load_rules_from_yaml(yaml_content: str) -> RuleEngine:
    """Parse declarative rules from a YAML string and return a RuleEngine.

    Raises:
        ValueError: If YAML is malformed, not a mapping, or lacks a 'rules' sequence.
    """
    try:
        data = yaml.safe_load(yaml_content)
    except Exception as exc:
        raise ValueError(f"Failed to parse YAML content: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Invalid rule configuration: top-level YAML must be a mapping")

    if "rules" not in data or not isinstance(data["rules"], list):
        raise ValueError("Invalid rule configuration: missing 'rules' list in YAML")

    return RuleEngine.from_dict(data)


def load_rules_from_file(path: str | Path) -> RuleEngine:
    """Read a YAML rule file from disk and return a compiled RuleEngine."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Triage rule configuration file not found: {file_path}")

    content = file_path.read_text(encoding="utf-8")
    return load_rules_from_yaml(content)


class HotReloadableRuleEngine:
    """Thread-safe and fail-safe hot-reloadable rule engine wrapper.

    Monitors file modification time (mtime) and automatically re-compiles rules
    on access. If an updated file contains invalid syntax or malformed regex, the
    engine logs an error and retains the previous valid ruleset without crashing.
    """

    def __init__(
        self,
        rules_path: str | Path | None = None,
        auto_reload: bool = True,
        initial_rules: list[Rule] | None = None,
    ) -> None:
        self._rules_path: Path | None = Path(rules_path) if rules_path else None
        self._auto_reload: bool = auto_reload
        self._last_mtime: float = 0.0

        if initial_rules is not None:
            self._engine: RuleEngine = RuleEngine(rules=initial_rules)
        elif self._rules_path and self._rules_path.exists():
            try:
                self._engine = load_rules_from_file(self._rules_path)
                self._last_mtime = self._rules_path.stat().st_mtime
                logger.info(
                    "Initialized HotReloadableRuleEngine from %s with %d rules",
                    self._rules_path,
                    len(self._engine.rules),
                )
            except Exception as exc:
                logger.error(
                    "Failed to load initial triage rules from %s: %s. Using empty engine.",
                    self._rules_path,
                    exc,
                )
                self._engine = RuleEngine()
        else:
            self._engine = RuleEngine()

    @property
    def rules_path(self) -> Path | None:
        """Configured path to declarative rules file."""
        return self._rules_path

    @property
    def rules_count(self) -> int:
        """Number of active compiled rules."""
        return len(self._engine.rules)

    @property
    def active_engine(self) -> RuleEngine:
        """Active compiled domain RuleEngine instance."""
        return self._engine

    def reload(self, force: bool = False) -> bool:
        """Check for file modification and reload rules if changed.

        Returns:
            bool: True if rules were reloaded, False if unchanged or reload failed.
        """
        if self._rules_path is None or not self._rules_path.exists():
            return False

        try:
            current_mtime = self._rules_path.stat().st_mtime
            if not force and current_mtime <= self._last_mtime:
                return False

            new_engine = load_rules_from_file(self._rules_path)
            self._engine = new_engine
            self._last_mtime = current_mtime
            logger.info(
                "Successfully reloaded triage rules from %s (%d rules active)",
                self._rules_path,
                len(self._engine.rules),
            )
            return True
        except Exception as exc:
            logger.error(
                "Failed to hot-reload triage rules from %s: %s. "
                "Retaining previous valid ruleset (%d rules).",
                self._rules_path,
                exc,
                len(self._engine.rules),
            )
            return False

    def evaluate(
        self,
        message: NormalizedMessage | EmailContext | dict[str, Any],
    ) -> Classification | None:
        """Evaluate an inbound email message, checking for updates first if auto_reload is on."""
        if self._auto_reload:
            self.reload()

        return self._engine.evaluate(message)
