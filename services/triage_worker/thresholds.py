"""Configurable triage confidence threshold resolution engine (R6.2, R6.9).

Provides hierarchical, runtime-reconfigurable confidence threshold lookups
across organization and category dimensions without requiring redeploy:
Precedence: Org+Category > Org Default > Global Category > Global Default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from packages.core.settings import TriageSettings

logger = logging.getLogger(__name__)

SUPPORTED_STAGES: frozenset[str] = frozenset({"rule", "ml", "llm"})


def _to_uuid_str(val: UUID | str | None) -> str | None:
    if val is None:
        return None
    return str(val)


@dataclass
class OrganizationThresholdOverrides:
    """Per-tenant threshold configuration overrides (R6.9)."""

    default_thresholds: dict[str, float] = field(default_factory=dict)
    category_thresholds: dict[str, dict[str, float]] = field(default_factory=dict)


class ThresholdManager:
    """Dynamic triage threshold manager enforcing hierarchical resolution (R6.9)."""

    def __init__(
        self,
        settings: TriageSettings | None = None,
        category_overrides: dict[str, dict[str, float]] | None = None,
    ) -> None:
        triage_settings = settings or TriageSettings()

        self._global_defaults: dict[str, float] = {
            "rule": float(triage_settings.rule_confidence_threshold),
            "ml": float(triage_settings.ml_confidence_threshold),
            "llm": float(triage_settings.llm_confidence_threshold),
        }

        # Global per-category overrides: category -> {stage -> threshold}
        self._global_categories: dict[str, dict[str, float]] = {}
        if category_overrides:
            for cat, stage_map in category_overrides.items():
                self._global_categories[cat.lower()] = {
                    s.lower(): float(t)
                    for s, t in stage_map.items()
                    if s.lower() in SUPPORTED_STAGES
                }

        # Per-organization overrides: org_id_str -> OrganizationThresholdOverrides
        self._org_overrides: dict[str, OrganizationThresholdOverrides] = {}

    def get_threshold(
        self,
        stage: str,
        category: str | None = None,
        organization_id: UUID | str | None = None,
    ) -> float:
        """Resolve the effective confidence threshold for a given stage, category, and org.

        Hierarchy (highest to lowest precedence):
        1. Organization-specific category threshold: org.categories[category][stage]
        2. Organization-specific stage default: org.default[stage]
        3. Global category threshold: global_categories[category][stage]
        4. Global stage default: global_defaults[stage]
        """
        stage_norm = stage.strip().lower()
        if stage_norm not in SUPPORTED_STAGES:
            supported = sorted(SUPPORTED_STAGES)
            raise ValueError(f"Unknown triage stage '{stage}'; supported: {supported}")

        cat_norm = category.strip().lower() if category else None
        org_key = _to_uuid_str(organization_id)

        # 1 & 2: Check organization overrides if org_key exists
        if org_key and org_key in self._org_overrides:
            org_override = self._org_overrides[org_key]

            # 1. Org-specific category threshold
            if cat_norm and cat_norm in org_override.category_thresholds:
                cat_stages = org_override.category_thresholds[cat_norm]
                if stage_norm in cat_stages:
                    return cat_stages[stage_norm]

            # 2. Org-specific stage default
            if stage_norm in org_override.default_thresholds:
                return org_override.default_thresholds[stage_norm]

        # 3. Global category threshold
        if cat_norm and cat_norm in self._global_categories:
            global_cat_stages = self._global_categories[cat_norm]
            if stage_norm in global_cat_stages:
                return global_cat_stages[stage_norm]

        # 4. Global stage default
        return self._global_defaults.get(stage_norm, 0.80)

    def set_global_default_threshold(self, stage: str, threshold: float) -> None:
        """Update global default threshold for a stage at runtime without restart."""
        stage_norm = stage.strip().lower()
        if stage_norm not in SUPPORTED_STAGES:
            raise ValueError(f"Unknown stage '{stage}'")
        self._validate_threshold(threshold)
        self._global_defaults[stage_norm] = float(threshold)

    def set_global_category_threshold(self, category: str, stage: str, threshold: float) -> None:
        """Update global category-specific threshold at runtime."""
        stage_norm = stage.strip().lower()
        if stage_norm not in SUPPORTED_STAGES:
            raise ValueError(f"Unknown stage '{stage}'")
        self._validate_threshold(threshold)
        cat_norm = category.strip().lower()
        if cat_norm not in self._global_categories:
            self._global_categories[cat_norm] = {}
        self._global_categories[cat_norm][stage_norm] = float(threshold)

    def set_organization_threshold(
        self,
        organization_id: UUID | str,
        stage: str,
        threshold: float,
    ) -> None:
        """Set tenant-specific default threshold for a stage without redeploy (R6.9)."""
        stage_norm = stage.strip().lower()
        if stage_norm not in SUPPORTED_STAGES:
            raise ValueError(f"Unknown stage '{stage}'")
        self._validate_threshold(threshold)
        org_key = str(_to_uuid_str(organization_id))

        if org_key not in self._org_overrides:
            self._org_overrides[org_key] = OrganizationThresholdOverrides()
        self._org_overrides[org_key].default_thresholds[stage_norm] = float(threshold)

    def set_organization_category_threshold(
        self,
        organization_id: UUID | str,
        category: str,
        stage: str,
        threshold: float,
    ) -> None:
        """Set tenant-specific category threshold without redeploy (R6.9)."""
        stage_norm = stage.strip().lower()
        if stage_norm not in SUPPORTED_STAGES:
            raise ValueError(f"Unknown stage '{stage}'")
        self._validate_threshold(threshold)
        org_key = str(_to_uuid_str(organization_id))
        cat_norm = category.strip().lower()

        if org_key not in self._org_overrides:
            self._org_overrides[org_key] = OrganizationThresholdOverrides()
        if cat_norm not in self._org_overrides[org_key].category_thresholds:
            self._org_overrides[org_key].category_thresholds[cat_norm] = {}
        self._org_overrides[org_key].category_thresholds[cat_norm][stage_norm] = float(threshold)

    def load_organization_settings(
        self,
        organization_id: UUID | str,
        settings_dict: dict[str, Any],
    ) -> None:
        """Load tenant thresholds dynamically from DB organization.settings or API payload."""
        org_key = str(_to_uuid_str(organization_id))
        triage_config = settings_dict.get("triage", settings_dict)

        overrides = OrganizationThresholdOverrides()

        # Parse defaults: e.g. {"rule_confidence_threshold": 0.96, "ml": 0.85}
        for key, val in triage_config.items():
            if val is None or not isinstance(val, (int, float)):
                continue
            cleaned_stage = None
            if key in SUPPORTED_STAGES:
                cleaned_stage = key
            elif key.endswith("_confidence_threshold"):
                prefix = key.replace("_confidence_threshold", "")
                if prefix in SUPPORTED_STAGES:
                    cleaned_stage = prefix
            if cleaned_stage:
                overrides.default_thresholds[cleaned_stage] = float(val)

        # Parse category overrides: e.g. {"category_thresholds": {"billing": {"ml": 0.90}}}
        cat_section = triage_config.get("category_thresholds", {})
        if isinstance(cat_section, dict):
            for cat, stage_map in cat_section.items():
                if isinstance(stage_map, dict):
                    overrides.category_thresholds[cat.lower()] = {
                        s.lower(): float(t)
                        for s, t in stage_map.items()
                        if s.lower() in SUPPORTED_STAGES and isinstance(t, (int, float))
                    }

        self._org_overrides[org_key] = overrides
        logger.info(
            "Loaded runtime threshold overrides for org %s: defaults=%s, categories=%s",
            org_key,
            overrides.default_thresholds,
            list(overrides.category_thresholds.keys()),
        )

    def clear_organization_overrides(self, organization_id: UUID | str | None = None) -> None:
        """Clear tenant threshold overrides (or all tenant overrides if org is None)."""
        if organization_id is None:
            self._org_overrides.clear()
        else:
            org_key = _to_uuid_str(organization_id)
            if org_key and org_key in self._org_overrides:
                del self._org_overrides[org_key]

    @staticmethod
    def _validate_threshold(val: float) -> None:
        if not (0.0 <= val <= 1.0):
            raise ValueError(f"Threshold must be in range [0.0, 1.0], got {val}")
