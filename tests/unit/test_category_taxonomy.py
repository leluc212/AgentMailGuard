"""Unit tests for the canonical category taxonomy (R6.4, design.md §5.3).

Validates:
- All 9 mandatory categories specified by R6.4 are present.
- Metadata definitions, default routing rules, and intent sets.
- Normalization, alias resolution, and validation.
- TaxonomyRegistry extensibility for tenant-specific custom categories.
- Architectural boundary conformance (stdlib + core only).
"""

import sys
from enum import StrEnum

import pytest

from packages.domain.taxonomy import (
    CANONICAL_CATEGORIES,
    CANONICAL_DEFINITIONS,
    NO_REPLY_CATEGORIES,
    RETRIEVAL_CATEGORIES,
    Category,
    CategoryDefinition,
    TaxonomyRegistry,
    get_category_definition,
    get_default_registry,
    is_valid_category,
    normalize_category,
    validate_category,
)


class TestCanonicalCategories:
    """Validate conformance with R6.4 mandatory category requirements."""

    MANDATORY_CATEGORIES = {
        "support",
        "sales",
        "billing",
        "administration",
        "scheduling",
        "general_inquiry",
        "automated_notification",
        "acknowledgement",
        "no_response",
    }

    def test_r6_4_mandatory_categories_present(self) -> None:
        """Assert that all 9 categories required by R6.4 exist in the Category enum."""
        enum_values = {c.value for c in Category}
        assert self.MANDATORY_CATEGORIES.issubset(enum_values)
        assert enum_values == self.MANDATORY_CATEGORIES
        assert len(CANONICAL_CATEGORIES) == 9

    def test_category_is_str_enum(self) -> None:
        """Category must be a StrEnum and strings compare directly."""
        assert issubclass(Category, StrEnum)
        assert Category.SUPPORT.value == "support"
        assert Category.BILLING.value == "billing"
        assert Category.GENERAL_INQUIRY.value == "general_inquiry"
        assert str(Category.SUPPORT) == "support"

    def test_all_canonical_categories_have_definitions(self) -> None:
        """Every canonical category must have an associated CategoryDefinition."""
        for cat in CANONICAL_CATEGORIES:
            defn = get_category_definition(cat)
            assert defn is not None
            assert defn.category == cat
            assert len(defn.description) > 10
            assert len(defn.intents) > 0


class TestCategoryRoutingDefaults:
    """Validate behavioral attributes matching design.md §5.3 and proposal §11."""

    def test_no_reply_categories(self) -> None:
        """Categories where automated reply is not required (R6.5)."""
        expected_no_reply = {"automated_notification", "acknowledgement", "no_response"}
        assert expected_no_reply == NO_REPLY_CATEGORIES

        for cat in expected_no_reply:
            defn = CANONICAL_DEFINITIONS[cat]
            assert defn.default_reply_required is False
            assert defn.default_workflow_hint == "none"

    def test_actionable_reply_categories(self) -> None:
        """Categories requiring an automated or drafted reply."""
        actionable = {
            "support",
            "sales",
            "billing",
            "administration",
            "scheduling",
            "general_inquiry",
        }
        for cat in actionable:
            defn = CANONICAL_DEFINITIONS[cat]
            assert defn.default_reply_required is True
            assert cat not in NO_REPLY_CATEGORIES

    def test_retrieval_categories(self) -> None:
        """Categories requiring enterprise knowledge retrieval (R6.6)."""
        expected_retrieval = {"support", "sales", "billing", "administration", "general_inquiry"}
        assert expected_retrieval == RETRIEVAL_CATEGORIES

        for cat in expected_retrieval:
            defn = CANONICAL_DEFINITIONS[cat]
            assert defn.default_retrieval_required is True

        # Non-retrieval categories
        non_retrieval = {"scheduling", "automated_notification", "acknowledgement", "no_response"}
        for cat in non_retrieval:
            defn = CANONICAL_DEFINITIONS[cat]
            assert defn.default_retrieval_required is False

    def test_default_workflow_hints(self) -> None:
        """Verify workflow hints for template vs AI vs none (R6.12)."""
        assert CANONICAL_DEFINITIONS["administration"].default_workflow_hint == "template"
        assert CANONICAL_DEFINITIONS["scheduling"].default_workflow_hint == "template"
        assert CANONICAL_DEFINITIONS["support"].default_workflow_hint == "ai"
        assert CANONICAL_DEFINITIONS["sales"].default_workflow_hint == "ai"
        assert CANONICAL_DEFINITIONS["billing"].default_workflow_hint == "ai"
        assert CANONICAL_DEFINITIONS["general_inquiry"].default_workflow_hint == "ai"

    def test_definition_to_dict(self) -> None:
        """CategoryDefinition must serialize to clean dictionary."""
        defn = CANONICAL_DEFINITIONS["billing"]
        d = defn.to_dict()
        assert d["category"] == "billing"
        assert d["default_reply_required"] is True
        assert d["default_retrieval_required"] is True
        assert "invoice_inquiry" in d["intents"]
        assert "invoice" in d["aliases"]


class TestCategoryNormalizationAndAliases:
    """Validate robust normalization, case handling, and alias mapping."""

    @pytest.mark.parametrize(
        ("input_val", "expected"),
        [
            ("support", "support"),
            ("SUPPORT", "support"),
            ("  support  ", "support"),
            ("technical_support", "support"),
            ("tech_support", "support"),
            ("bug", "support"),
            ("issue", "support"),
            ("sales", "sales"),
            ("lead", "sales"),
            ("pricing", "sales"),
            ("quote", "sales"),
            ("billing", "billing"),
            ("invoice", "billing"),
            ("payment", "billing"),
            ("finance", "billing"),
            ("administration", "administration"),
            ("admin", "administration"),
            ("account", "administration"),
            ("scheduling", "scheduling"),
            ("calendar", "scheduling"),
            ("meeting", "scheduling"),
            ("general_inquiry", "general_inquiry"),
            ("general-inquiry", "general_inquiry"),
            ("general inquiry", "general_inquiry"),
            ("faq", "general_inquiry"),
            ("automated_notification", "automated_notification"),
            ("notification", "automated_notification"),
            ("newsletter", "automated_notification"),
            ("acknowledgement", "acknowledgement"),
            ("ack", "acknowledgement"),
            ("thank_you", "acknowledgement"),
            ("no_response", "no_response"),
            ("out_of_office", "no_response"),
            ("ooo", "no_response"),
            ("spam", "no_response"),
        ],
    )
    def test_normalize_known_aliases(self, input_val: str, expected: str) -> None:
        """All common synonyms and casing variants must normalize to canonical category."""
        assert normalize_category(input_val) == expected

    def test_is_valid_category(self) -> None:
        """Valid category returns True for canonical and aliases, False for unknown."""
        assert is_valid_category("billing") is True
        assert is_valid_category("INVOICE") is True
        assert is_valid_category(Category.SUPPORT) is True
        assert is_valid_category("random_unrelated_gibberish") is False
        assert is_valid_category("") is False

    def test_validate_category_success(self) -> None:
        """Valid category passes through validate_category and returns canonical string."""
        assert validate_category("tech_support") == "support"
        assert validate_category(Category.SALES) == "sales"
        assert validate_category("  BILLING  ") == "billing"

    def test_validate_category_failure(self) -> None:
        """Invalid category raises ValueError with descriptive message."""
        with pytest.raises(ValueError, match="Unknown category 'unknown_category'"):
            validate_category("unknown_category")


class TestTaxonomyRegistry:
    """Validate TaxonomyRegistry extensibility and isolation."""

    def test_default_registry_contains_canonical(self) -> None:
        """Global registry has all 9 canonical categories."""
        registry = get_default_registry()
        for cat in CANONICAL_CATEGORIES:
            assert registry.is_valid(cat) is True
            assert registry.is_canonical(cat) is True

    def test_custom_tenant_category_registration(self) -> None:
        """A tenant can register a domain-specific category without breaking canonical set."""
        registry = TaxonomyRegistry()
        custom_defn = CategoryDefinition(
            category="custom_compliance",
            description="Tenant-specific regulatory compliance emails.",
            default_reply_required=True,
            default_retrieval_required=True,
            default_workflow_hint="ai",
            default_priority="urgent",
            intents=("gdpr_request", "audit_notice"),
            aliases=("compliance", "legal_audit"),
        )
        registry.register_category(custom_defn)

        assert registry.is_valid("custom_compliance") is True
        assert registry.is_valid("COMPLIANCE") is True
        assert registry.normalize("legal_audit") == "custom_compliance"
        assert registry.is_canonical("custom_compliance") is False
        assert registry.validate("compliance") == "custom_compliance"

        # Canonical categories are still present and intact
        assert registry.is_valid("support") is True
        assert registry.is_canonical("support") is True

    def test_empty_category_registration_fails(self) -> None:
        """Registering an empty category name raises ValueError."""
        registry = TaxonomyRegistry()
        with pytest.raises(ValueError, match="Category name cannot be empty"):
            registry.register_category(
                CategoryDefinition(
                    category="",
                    description="invalid",
                    default_reply_required=False,
                    default_retrieval_required=False,
                    default_workflow_hint="none",
                )
            )


class TestArchitecturalIsolation:
    """Ensure packages.domain.taxonomy adheres strictly to domain boundary rules."""

    def test_taxonomy_imports_stdlib_only(self) -> None:
        """taxonomy.py must not import external libraries or other packages."""
        import packages.domain.taxonomy as mod

        allowed_stdlib = set(sys.stdlib_module_names)
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if hasattr(attr, "__module__") and attr.__module__:
                module_name = attr.__module__
                top_module = module_name.split(".")[0]
                assert (
                    top_module in allowed_stdlib
                    or module_name.startswith("packages.domain")
                    or module_name.startswith("packages.core")
                ), f"Prohibited import in taxonomy: {module_name} ({attr})"
