"""Unit tests for Agent Profiles, Registry, and Jinja2 Prompt Templates (R14.1, R14.2, R14.6)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


def test_schema_file_valid_json() -> None:
    """Verify schemas/reply.v1.json conforms to draft reply schema requirements.

    Covers design.md §5.7 and R16.1.
    """
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
        "prompts/support.v3.j2",
        "prompts/billing.v3.j2",
        "prompts/sales.v3.j2",
        "prompts/general.v3.j2",
    ]
    for tmpl in expected_templates:
        p = Path(tmpl)
        assert p.is_file(), f"Template {tmpl} must exist"
        content = p.read_text(encoding="utf-8")
        assert "{{ agent_instructions }}" in content or "{{ instructions }}" in content
        assert "current_message" in content


def test_agent_profiles_yaml_valid() -> None:
    """Verify config/agent_profiles.yaml is syntactically valid and defines profiles (R14.1)."""
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
    assert tech["prompt_template"] == "prompts/support.v3.j2"
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


def test_registry_resolve_by_category() -> None:
    """Verify category resolution to specialized profiles (R14.1, R14.2)."""
    from packages.llm.profile import AgentProfileRegistry

    registry = AgentProfileRegistry.from_yaml("config/agent_profiles.yaml")
    support_prof = registry.resolve_profile("support")
    assert support_prof.profile == "technical_support"
    assert support_prof.knowledge_domain == "support"
    assert support_prof.prompt_version == "support.v3"

    billing_prof = registry.resolve_profile("billing")
    assert billing_prof.profile == "billing"
    assert billing_prof.knowledge_domain == "billing"


def test_registry_fallback_to_default() -> None:
    """Verify unmapped or unknown category falls back to default profile (R14.2)."""
    from packages.llm.profile import AgentProfileRegistry

    registry = AgentProfileRegistry.from_yaml("config/agent_profiles.yaml")
    fallback_prof = registry.resolve_profile("non_existent_category")
    assert fallback_prof.profile == "general_inquiry"

    fallback_none = registry.resolve_profile(None)
    assert fallback_none.profile == "general_inquiry"


def test_registry_schema_loading() -> None:
    """Verify output schema resolution from file or dict (R14.1)."""
    from packages.llm.profile import AgentProfileRegistry

    registry = AgentProfileRegistry.from_yaml("config/agent_profiles.yaml")
    profile = registry.get_profile("technical_support")
    assert profile is not None

    schema = registry.get_schema(profile)
    assert isinstance(schema, dict)
    assert "properties" in schema
    assert "draft" in schema["required"]


def test_registry_render_prompt_with_context_package() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from packages.domain.business import (
        BusinessContext,
        BusinessFact,
        CustomerStatus,
        EntityType,
        FactStatus,
    )
    from packages.domain.entities import Candidate, ContextPackage, EmailAddress, NormalizedMessage
    from packages.llm.profile import AgentProfileRegistry

    registry = AgentProfileRegistry.from_yaml("config/agent_profiles.yaml")
    profile = registry.resolve_profile("support")

    org_id = uuid4()
    msg = NormalizedMessage(
        message_id=uuid4(),
        organization_id=org_id,
        mailbox_id=uuid4(),
        thread_id=uuid4(),
        provider="mock",
        provider_message_id="msg-1234",
        sender=EmailAddress(email="dev@example.com", name="Lead Dev"),
        subject="Database connection failure",
        subject_normalized="Database connection failure",
        body_text="Unable to connect to database at host db.local:5432.",
        body_text_clean="Unable to connect to database at host db.local:5432.",
        received_at=datetime.now(UTC),
    )
    chunk = Candidate(
        chunk_id="chunk-1",
        document_id="doc-1",
        content="PostgreSQL listen_addresses must include target interface.",
        metadata={"title": "DB Guide"},
        lexical_rank=1,
        vector_rank=1,
        lexical_score=0.9,
        vector_score=0.85,
        fused_score=0.03,
        rerank_score=0.95,
        external_id="DOC-PG-01",
    )
    business = BusinessContext(
        customer_status=CustomerStatus.FOUND,
        as_of=datetime.now(UTC),
        customer=(("plan", "Enterprise"),),
        facts=(
            BusinessFact(
                entity=EntityType.TICKET, reference="TICK-4402", status=FactStatus.NOT_FOUND
            ),
        ),
    )
    context_pkg = ContextPackage(
        agent_instructions="You are an enterprise AI assistant.",
        category_instructions="Address technical questions with structured steps.",
        current_message=msg,
        retrieved_chunks=[chunk],
        business_data=business,
    )

    rendered = registry.render_prompt(profile, context_pkg)
    assert "You are an enterprise AI assistant." in rendered
    assert "Address technical questions with structured steps." in rendered
    assert "Database connection failure" in rendered
    assert "Unable to connect to database at host db.local:5432." in rendered
    assert "[CITATION: DOC-PG-01]" in rendered
    assert business.render() in rendered
    assert "Enterprise" in rendered


def test_registry_satisfies_instruction_provider_protocol() -> None:
    """Verify registry satisfies ContextBuilder's InstructionProvider protocol (R14.1, R14.8)."""
    from packages.context.builder import InstructionProvider
    from packages.llm.profile import AgentProfileRegistry

    registry = AgentProfileRegistry.from_yaml("config/agent_profiles.yaml")
    assert isinstance(registry, InstructionProvider)

    agent_instr, cat_instr = registry.get_instructions("support")
    assert "enterprise" in agent_instr.lower()
    assert "technical" in cat_instr.lower()


def test_agent_profile_settings_defaults() -> None:
    """Verify AgentProfileSettings defaults on AppSettings (R14.1, R14.2)."""
    from packages.core.settings import AppSettings

    settings = AppSettings()
    assert hasattr(settings, "agent_profiles")
    assert settings.agent_profiles.config_path == "config/agent_profiles.yaml"
    assert settings.agent_profiles.default_profile == "general_inquiry"
    assert settings.agent_profiles.prompts_dir == "prompts"
    assert settings.agent_profiles.schemas_dir == "schemas"
