"""Unit tests for Agent Profiles, Registry, and Jinja2 Prompt Templates (R14.1, R14.2, R14.6)."""

from __future__ import annotations

import json
from pathlib import Path
import yaml


def test_schema_file_valid_json() -> None:
    """Verify schemas/reply.v1.json conforms to draft reply schema requirements (design.md §5.7, R16.1)."""
    schema_path = Path("schemas/reply.v1.json")
    assert schema_path.is_file(), "schemas/reply.v1.json must exist"

    data = json.loads(schema_path.read_text(encoding="utf-8"))
    assert data["type"] == "object"
    assert "action" in data["properties"]
    assert data["properties"]["action"]["enum"] == ["reply", "forward", "escalate", "no_reply"]
    assert "draft" in data["properties"]
    assert "confidence" in data["properties"]
    assert "knowledge_chunks" in data["properties"]
    assert "draft" in data["required"]
    assert "confidence" in data["required"]
    assert "knowledge_chunks" in data["required"]


def test_prompt_template_files_exist() -> None:
    """Verify versioned prompt template files exist and contain core jinja2 markers (R14.6)."""
    expected_templates = [
        "prompts/support.v1.j2",
        "prompts/billing.v1.j2",
        "prompts/sales.v1.j2",
        "prompts/general.v1.j2",
    ]
    for tmpl in expected_templates:
        p = Path(tmpl)
        assert p.is_file(), f"Template {tmpl} must exist"
        content = p.read_text(encoding="utf-8")
        assert "{{ agent_instructions }}" in content or "{{ instructions }}" in content
        assert "current_message" in content


def test_agent_profiles_yaml_valid() -> None:
    """Verify config/agent_profiles.yaml is syntactically valid and defines core profiles (R14.1)."""
    config_path = Path("config/agent_profiles.yaml")
    assert config_path.is_file(), "config/agent_profiles.yaml must exist"

    content = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "profiles" in content
    profiles = content["profiles"]
    assert "technical_support" in profiles
    assert "billing" in profiles
    assert "sales" in profiles
    assert "general_inquiry" in profiles

    tech = profiles["technical_support"]
    assert tech["knowledge_domain"] == "support"
    assert tech["response_style"] == "professional"
    assert tech["model_tier"] == "routine"
    assert tech["context_policy"] == "thread_plus_rag"
    assert tech["prompt_template"] == "prompts/support.v1.j2"
    assert tech["output_schema"] == "schemas/reply.v1.json"


def test_agent_profile_creation() -> None:
    """Verify AgentProfile model instantiation with all required R14.1/R14.6 attributes."""
    from packages.llm.profile import AgentProfile, ContextPolicy
    from packages.llm.protocol import ModelTier

    profile = AgentProfile(
        profile="technical_support",
        knowledge_domain="support",
        response_style="professional",
        model_tier=ModelTier.ROUTINE,
        context_policy=ContextPolicy.THREAD_PLUS_RAG,
        prompt_template="prompts/support.v1.j2",
        output_schema="schemas/reply.v1.json",
        prompt_version="support.v1",
        categories=["support", "technical_support"],
        category_instructions="Troubleshoot technical issues.",
        description="Support profile",
    )
    assert profile.profile == "technical_support"
    assert profile.knowledge_domain == "support"
    assert profile.response_style == "professional"
    assert profile.model_tier == ModelTier.ROUTINE
    assert profile.context_policy == ContextPolicy.THREAD_PLUS_RAG
    assert profile.prompt_template == "prompts/support.v1.j2"
    assert profile.output_schema == "schemas/reply.v1.json"
    assert profile.prompt_version == "support.v1"
    assert profile.categories == ["support", "technical_support"]
    assert profile.category_instructions == "Troubleshoot technical issues."


def test_agent_profile_defaults() -> None:
    """Verify default values for optional AgentProfile fields."""
    from packages.llm.profile import AgentProfile, ContextPolicy
    from packages.llm.protocol import ModelTier

    profile = AgentProfile(
        profile="minimal_profile",
        knowledge_domain="general",
        response_style="concise",
        prompt_template="prompts/general.v1.j2",
        output_schema="schemas/reply.v1.json",
        prompt_version="general.v1",
    )
    assert profile.model_tier == ModelTier.ROUTINE
    assert profile.context_policy == ContextPolicy.THREAD_PLUS_RAG
    assert profile.categories == []
    assert profile.agent_instructions is None
    assert profile.category_instructions is None

