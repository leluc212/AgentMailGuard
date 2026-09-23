"""Canonical category taxonomy for email triage and routing (R6.4, design.md §5.3).

Requirements:
- R6.4: THE SYSTEM SHALL support at minimum the categories:
  support, sales, billing, administration, scheduling, general_inquiry,
  automated_notification, acknowledgement, no_response.
- GEMINI.md: packages/domain imports standard library and packages/core ONLY.
- design.md §5.3: Category set, default routing behaviors, and intent mappings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Category(StrEnum):
    """The 9 mandatory canonical triage categories defined by R6.4."""

    SUPPORT = "support"
    SALES = "sales"
    BILLING = "billing"
    ADMINISTRATION = "administration"
    SCHEDULING = "scheduling"
    GENERAL_INQUIRY = "general_inquiry"
    AUTOMATED_NOTIFICATION = "automated_notification"
    ACKNOWLEDGEMENT = "acknowledgement"
    NO_RESPONSE = "no_response"


@dataclass(frozen=True)
class CategoryDefinition:
    """Metadata specification for a classification category (R6.4, R6.5, R6.6, R6.12)."""

    category: str
    description: str
    default_reply_required: bool
    default_retrieval_required: bool
    default_workflow_hint: str  # ai | template | none
    default_priority: str = "normal"
    intents: tuple[str, ...] = field(default_factory=tuple)
    auto_send_eligible: bool = False
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Convert definition to dictionary."""
        return {
            "category": self.category,
            "description": self.description,
            "default_reply_required": self.default_reply_required,
            "default_retrieval_required": self.default_retrieval_required,
            "default_workflow_hint": self.default_workflow_hint,
            "default_priority": self.default_priority,
            "intents": list(self.intents),
            "auto_send_eligible": self.auto_send_eligible,
            "aliases": list(self.aliases),
        }


# Canonical definitions for all 9 mandatory categories per R6.4
CANONICAL_DEFINITIONS: dict[str, CategoryDefinition] = {
    Category.SUPPORT.value: CategoryDefinition(
        category=Category.SUPPORT.value,
        description="Technical bugs, crashes, incidents, or hardware/software troubleshooting.",
        default_reply_required=True,
        default_retrieval_required=True,
        default_workflow_hint="ai",
        default_priority="normal",
        intents=(
            "bug_report",
            "incident",
            "technical_troubleshooting",
            "feature_assistance",
        ),
        aliases=("technical_support", "tech_support", "bug", "issue", "troubleshooting"),
    ),
    Category.SALES.value: CategoryDefinition(
        category=Category.SALES.value,
        description=(
            "Volume quotes, pricing queries, demo requests, enterprise licensing, partnerships."
        ),
        default_reply_required=True,
        default_retrieval_required=True,
        default_workflow_hint="ai",
        default_priority="normal",
        intents=(
            "quote_request",
            "pricing_inquiry",
            "demo_request",
            "enterprise_licensing",
            "partnership",
        ),
        aliases=("lead", "pricing", "quote", "demo", "procurement"),
    ),
    Category.BILLING.value: CategoryDefinition(
        category=Category.BILLING.value,
        description="Invoices, payment receipts, charges, refunds, payment method updates.",
        default_reply_required=True,
        default_retrieval_required=True,
        default_workflow_hint="ai",
        default_priority="normal",
        intents=(
            "invoice_inquiry",
            "payment_failure",
            "refund_request",
            "tax_exemption",
            "receipt_lookup",
        ),
        aliases=("invoice", "payment", "finance", "subscription", "charge"),
    ),
    Category.ADMINISTRATION.value: CategoryDefinition(
        category=Category.ADMINISTRATION.value,
        description=(
            "Account setup, user provisioning, password resets, permissions, audit exports."
        ),
        default_reply_required=True,
        default_retrieval_required=True,
        default_workflow_hint="template",
        default_priority="normal",
        intents=(
            "password_reset",
            "user_provisioning",
            "permissions",
            "account_setup",
            "audit_export",
        ),
        aliases=("admin", "account", "access", "user_management", "provisioning"),
    ),
    Category.SCHEDULING.value: CategoryDefinition(
        category=Category.SCHEDULING.value,
        description=(
            "Meeting requests, calendar invitations, scheduling availability, demo bookings."
        ),
        default_reply_required=True,
        default_retrieval_required=False,
        default_workflow_hint="template",
        default_priority="normal",
        intents=(
            "meeting_request",
            "calendar_invitation",
            "reschedule",
            "availability",
            "meeting_accepted",
        ),
        aliases=("calendar", "meeting", "appointment", "booking"),
    ),
    Category.GENERAL_INQUIRY.value: CategoryDefinition(
        category=Category.GENERAL_INQUIRY.value,
        description="General questions about company, services, or unclassified general inquiries.",
        default_reply_required=True,
        default_retrieval_required=True,
        default_workflow_hint="ai",
        default_priority="normal",
        intents=(
            "general_faq",
            "company_info",
            "press_inquiry",
            "office_hours",
            "contact_info",
        ),
        aliases=("inquiry", "general", "faq", "info", "other", "general_question"),
    ),
    Category.AUTOMATED_NOTIFICATION.value: CategoryDefinition(
        category=Category.AUTOMATED_NOTIFICATION.value,
        description="System alerts, newsletters, CI/CD reports, noreply updates.",
        default_reply_required=False,
        default_retrieval_required=False,
        default_workflow_hint="none",
        default_priority="low",
        intents=(
            "system_alert",
            "newsletter",
            "ci_cd",
            "marketing",
            "status_update",
        ),
        aliases=("notification", "newsletter", "alert", "automated", "system_notification"),
    ),
    Category.ACKNOWLEDGEMENT.value: CategoryDefinition(
        category=Category.ACKNOWLEDGEMENT.value,
        description="Standalone thank-you emails, receipt/delivery confirmations.",
        default_reply_required=False,
        default_retrieval_required=False,
        default_workflow_hint="none",
        default_priority="low",
        intents=(
            "receipt_confirmation",
            "thank_you",
            "delivery_confirmation",
            "ticket_ack",
        ),
        aliases=("ack", "thank_you", "confirmation", "receipt", "received"),
    ),
    Category.NO_RESPONSE.value: CategoryDefinition(
        category=Category.NO_RESPONSE.value,
        description=(
            "Out-of-office autoreplies, vacation notices, spam, unsolicited promotional outreach."
        ),
        default_reply_required=False,
        default_retrieval_required=False,
        default_workflow_hint="none",
        default_priority="low",
        intents=(
            "out_of_office",
            "vacation_notice",
            "spam",
            "unsolicited",
            "bounce",
        ),
        aliases=("spam", "out_of_office", "ooo", "autoreply", "auto_reply", "ignore"),
    ),
}

# Immutable sets for quick category property checking
CANONICAL_CATEGORIES: frozenset[str] = frozenset(c.value for c in Category)

NO_REPLY_CATEGORIES: frozenset[str] = frozenset(
    cat for cat, defn in CANONICAL_DEFINITIONS.items() if not defn.default_reply_required
)

RETRIEVAL_CATEGORIES: frozenset[str] = frozenset(
    cat for cat, defn in CANONICAL_DEFINITIONS.items() if defn.default_retrieval_required
)

# Global lookup for known aliases
_DEFAULT_ALIASES: dict[str, str] = {}
for _cat, _defn in CANONICAL_DEFINITIONS.items():
    for _alias in _defn.aliases:
        _DEFAULT_ALIASES[_alias.lower().strip()] = _cat


class TaxonomyRegistry:
    """Registry maintaining available categories and tenant-specific extensions.

    Guarantees that at minimum the 9 canonical categories required by R6.4 are always present.
    """

    def __init__(self, initial_definitions: dict[str, CategoryDefinition] | None = None) -> None:
        self._definitions: dict[str, CategoryDefinition] = dict(CANONICAL_DEFINITIONS)
        self._aliases: dict[str, str] = dict(_DEFAULT_ALIASES)

        if initial_definitions:
            for defn in initial_definitions.values():
                self.register_category(defn)

    def register_category(self, definition: CategoryDefinition) -> None:
        """Register a category definition. Can add custom categories or update attributes."""
        if not definition.category or not definition.category.strip():
            raise ValueError("Category name cannot be empty")
        normalized_name = definition.category.strip().lower()
        self._definitions[normalized_name] = definition
        for alias in definition.aliases:
            self._aliases[alias.strip().lower()] = normalized_name

    def register_from_dict(self, data: dict[str, Any]) -> CategoryDefinition:
        """Parse dictionary definition and register into taxonomy (R7.4)."""
        if "category" not in data or not str(data["category"]).strip():
            raise ValueError("Category dictionary must contain non-empty 'category' key")
        category = str(data["category"]).strip().lower()
        defn = CategoryDefinition(
            category=category,
            description=str(data.get("description", "")),
            default_reply_required=bool(data.get("default_reply_required", True)),
            default_retrieval_required=bool(data.get("default_retrieval_required", True)),
            default_workflow_hint=str(data.get("default_workflow_hint", "ai")),
            default_priority=str(data.get("default_priority", "normal")),
            intents=tuple(data.get("intents", ())),
            auto_send_eligible=bool(data.get("auto_send_eligible", False)),
            aliases=tuple(data.get("aliases", ())),
        )
        self.register_category(defn)
        return defn

    def register_categories(
        self, definitions: list[CategoryDefinition | dict[str, Any]]
    ) -> list[CategoryDefinition]:
        """Register multiple category definitions (R7.4)."""
        results: list[CategoryDefinition] = []
        for item in definitions:
            if isinstance(item, CategoryDefinition):
                self.register_category(item)
                results.append(item)
            elif isinstance(item, dict):
                results.append(self.register_from_dict(item))
            else:
                raise TypeError(f"Expected CategoryDefinition or dict, got {type(item)}")
        return results

    def get(self, category: str | Category) -> CategoryDefinition | None:
        """Retrieve category definition by canonical name or alias."""
        normalized = self.normalize(category)
        return self._definitions.get(normalized)

    def normalize(self, category: str | Category) -> str:
        """Normalize a category string: trims whitespace, lowercases, resolves aliases."""
        raw = str(category.value if isinstance(category, Category) else category).strip().lower()
        # Clean any underscores/hyphens/spaces
        cleaned = raw.replace("-", "_").replace(" ", "_")
        return self._aliases.get(cleaned, cleaned)

    def is_valid(self, category: str | Category) -> bool:
        """Check if a category or alias is recognized in this taxonomy."""
        normalized = self.normalize(category)
        return normalized in self._definitions

    def is_canonical(self, category: str | Category) -> bool:
        """Check if category is one of the 9 baseline canonical categories from R6.4."""
        normalized = self.normalize(category)
        return normalized in CANONICAL_CATEGORIES

    def validate(self, category: str | Category) -> str:
        """Validate category and return its canonical form, raising ValueError if invalid."""
        normalized = self.normalize(category)
        if normalized not in self._definitions:
            valid_keys = sorted(self._definitions.keys())
            raise ValueError(f"Unknown category '{category}'. Must be one of: {valid_keys}")
        return normalized

    def all_categories(self) -> list[str]:
        """List all registered category names."""
        return sorted(self._definitions.keys())


# Singleton default registry instance
_DEFAULT_REGISTRY = TaxonomyRegistry()


def get_default_registry() -> TaxonomyRegistry:
    """Return the global default category taxonomy registry."""
    return _DEFAULT_REGISTRY


def normalize_category(category: str | Category) -> str:
    """Normalize category name via default registry."""
    return _DEFAULT_REGISTRY.normalize(category)


def is_valid_category(category: str | Category) -> bool:
    """Check if category is recognized via default registry."""
    return _DEFAULT_REGISTRY.is_valid(category)


def validate_category(category: str | Category) -> str:
    """Validate category name via default registry or raise ValueError."""
    return _DEFAULT_REGISTRY.validate(category)


def get_category_definition(category: str | Category) -> CategoryDefinition | None:
    """Retrieve category definition from default registry."""
    return _DEFAULT_REGISTRY.get(category)
