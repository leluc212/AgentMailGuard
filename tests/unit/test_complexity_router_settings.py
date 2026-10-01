"""Unit tests for ComplexityRouterSettings and AppSettings integration.

References: R15.1, R15.6, design.md §5.7.
"""

import pytest
from pydantic import ValidationError

from packages.core import ComplexityRouterSettings
from packages.core.settings import AppSettings


def test_complexity_router_settings_default_values() -> None:
    """Verify ComplexityRouterSettings default configuration values (R15.1, R15.6)."""
    settings = ComplexityRouterSettings()

    assert settings.enabled is True
    assert settings.force_single_tier is False
    assert settings.default_tier == "routine"
    assert settings.escalated_tier == "high_capability"
    assert settings.confidence_threshold == 0.75
    assert settings.thread_messages_threshold == 5
    assert settings.thread_tokens_threshold == 2000
    assert settings.min_retrieved_chunks == 2
    assert settings.min_relevance_score == 0.50
    assert settings.multiple_actions_threshold == 2
    assert settings.context_tokens_threshold == 3500
    assert settings.max_escalations_per_job == 1
    assert settings.single_tier_override == "high_capability"


def test_complexity_router_settings_custom_overrides() -> None:
    """Verify custom instantiation overrides default values."""
    settings = ComplexityRouterSettings(
        force_single_tier=True,
        confidence_threshold=0.80,
        thread_messages_threshold=10,
        thread_tokens_threshold=4000,
        min_retrieved_chunks=3,
        min_relevance_score=0.60,
        multiple_actions_threshold=3,
        context_tokens_threshold=5000,
        max_escalations_per_job=2,
        default_tier="fast",
        escalated_tier="strong",
        single_tier_override="strong",
        enabled=False,
    )

    assert settings.enabled is False
    assert settings.force_single_tier is True
    assert settings.default_tier == "fast"
    assert settings.escalated_tier == "strong"
    assert settings.confidence_threshold == 0.80
    assert settings.thread_messages_threshold == 10
    assert settings.thread_tokens_threshold == 4000
    assert settings.min_retrieved_chunks == 3
    assert settings.min_relevance_score == 0.60
    assert settings.multiple_actions_threshold == 3
    assert settings.context_tokens_threshold == 5000
    assert settings.max_escalations_per_job == 2
    assert settings.single_tier_override == "strong"


def test_app_settings_complexity_router_default() -> None:
    """Verify AppSettings mounts complexity_router with default values."""
    app_settings = AppSettings(_env_file=None)

    assert hasattr(app_settings, "complexity_router")
    router = app_settings.complexity_router
    assert isinstance(router, ComplexityRouterSettings)
    assert router.enabled is True
    assert router.force_single_tier is False
    assert router.default_tier == "routine"
    assert router.escalated_tier == "high_capability"
    assert router.confidence_threshold == 0.75
    assert router.thread_messages_threshold == 5
    assert router.thread_tokens_threshold == 2000
    assert router.min_retrieved_chunks == 2
    assert router.min_relevance_score == 0.50
    assert router.multiple_actions_threshold == 2
    assert router.context_tokens_threshold == 3500
    assert router.max_escalations_per_job == 1
    assert router.single_tier_override == "high_capability"


def test_app_settings_complexity_router_custom_dict() -> None:
    """Verify AppSettings accepts custom complexity_router dictionary."""
    app_settings = AppSettings(
        _env_file=None,
        complexity_router={
            "force_single_tier": True,
            "confidence_threshold": 0.85,
        },
    )
    assert app_settings.complexity_router.force_single_tier is True
    assert app_settings.complexity_router.confidence_threshold == 0.85
    assert app_settings.complexity_router.enabled is True


def test_app_settings_complexity_router_custom_instance() -> None:
    """Verify AppSettings accepts custom ComplexityRouterSettings instance."""
    custom_router = ComplexityRouterSettings(
        enabled=False,
        force_single_tier=True,
        confidence_threshold=0.90,
    )
    app_settings = AppSettings(_env_file=None, complexity_router=custom_router)
    assert app_settings.complexity_router.enabled is False
    assert app_settings.complexity_router.force_single_tier is True
    assert app_settings.complexity_router.confidence_threshold == 0.90


def test_app_settings_flat_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify flat ROUTER_* environment variables override router settings."""
    monkeypatch.setenv("ROUTER_FORCE_SINGLE_TIER", "true")
    monkeypatch.setenv("ROUTER_CONFIDENCE_THRESHOLD", "0.82")
    monkeypatch.setenv("ROUTER_THREAD_MESSAGES_THRESHOLD", "8")

    app_settings = AppSettings(_env_file=None)
    assert app_settings.complexity_router.force_single_tier is True
    assert app_settings.complexity_router.confidence_threshold == 0.82
    assert app_settings.complexity_router.thread_messages_threshold == 8


def test_app_settings_nested_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify nested ROUTER__* environment variables override router settings."""
    monkeypatch.setenv("ROUTER__ENABLED", "false")
    monkeypatch.setenv("ROUTER__CONFIDENCE_THRESHOLD", "0.88")

    app_settings = AppSettings(_env_file=None)
    assert app_settings.complexity_router.enabled is False
    assert app_settings.complexity_router.confidence_threshold == 0.88


def test_fail_fast_on_invalid_router_confidence_threshold() -> None:
    """Verify fail-fast validation on illegal confidence thresholds (< 0 or > 1.0)."""
    with pytest.raises(ValidationError):
        ComplexityRouterSettings(confidence_threshold=1.5)

    with pytest.raises(ValidationError):
        ComplexityRouterSettings(confidence_threshold=-0.1)


def test_fail_fast_on_invalid_router_relevance_score() -> None:
    """Verify fail-fast validation on illegal relevance scores (< 0 or > 1.0)."""
    with pytest.raises(ValidationError):
        ComplexityRouterSettings(min_relevance_score=1.1)

    with pytest.raises(ValidationError):
        ComplexityRouterSettings(min_relevance_score=-0.05)


def test_fail_fast_on_invalid_router_threshold_counts() -> None:
    """Verify fail-fast validation on negative or zero thresholds where disallowed."""
    with pytest.raises(ValidationError):
        ComplexityRouterSettings(thread_messages_threshold=0)

    with pytest.raises(ValidationError):
        ComplexityRouterSettings(thread_tokens_threshold=0)

    with pytest.raises(ValidationError):
        ComplexityRouterSettings(context_tokens_threshold=0)

    with pytest.raises(ValidationError):
        ComplexityRouterSettings(multiple_actions_threshold=0)

    with pytest.raises(ValidationError):
        ComplexityRouterSettings(min_retrieved_chunks=-1)

    with pytest.raises(ValidationError):
        ComplexityRouterSettings(max_escalations_per_job=-1)


def test_core_package_re_export() -> None:
    """Verify ComplexityRouterSettings is re-exported from packages.core."""
    import packages.core

    assert hasattr(packages.core, "ComplexityRouterSettings")
    assert "ComplexityRouterSettings" in packages.core.__all__
