"""Agent profile definitions, context policies, and profile registry (R14.1, R14.2, R14.6).

Specialization by configuration rather than chained multi-agent pipelines (R14.4, design.md §5.7).
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

from packages.llm.protocol import ModelTier

if TYPE_CHECKING:
    from packages.domain.entities import ContextPackage

logger = logging.getLogger(__name__)

DEFAULT_ENTERPRISE_INSTRUCTIONS = (
    "You are an enterprise AI assistant for customer email correspondence. "
    "Provide professional, concise, and helpful responses grounded in the "
    "provided thread history and reference knowledge."
)


class ContextPolicy(StrEnum):
    """Context assembly policies for generation (R14.1, design.md §5.7)."""

    THREAD_PLUS_RAG = "thread_plus_rag"
    THREAD_PLUS_RAG_PLUS_BUSINESS = "thread_plus_rag_plus_business"
    THREAD_ONLY = "thread_only"
    RAG_ONLY = "rag_only"


class AgentProfile(BaseModel):
    """Agent profile specifying domain specialization and prompt contracts (R14.1, R14.6).

    Attributes:
        profile: Unique identifier for the profile (e.g. 'technical_support', 'billing').
        knowledge_domain: Domain identifier for targeted knowledge retrieval (e.g. 'support').
        response_style: Target tone/style (e.g. 'professional', 'precise_formal', 'concise').
        model_tier: Required model capability tier (routine vs high_capability).
        context_policy: Context assembly policy (e.g. thread_plus_rag).
        prompt_template: Relative path or identifier of versioned Jinja2 prompt template.
        output_schema: Relative path or JSON schema dictionary for structured generation.
        prompt_version: Version identifier recorded on generated drafts (R14.6).
        categories: List of classification categories mapped to this profile.
        agent_instructions: Optional general instructions for the profile.
        category_instructions: Optional specific category instructions for prompt-prefix caching.
        description: Human-readable description of the profile's role.
    """

    profile: str
    knowledge_domain: str
    response_style: str
    model_tier: ModelTier = ModelTier.ROUTINE
    context_policy: ContextPolicy | str = ContextPolicy.THREAD_PLUS_RAG
    prompt_template: str
    output_schema: str | dict[str, Any]
    prompt_version: str
    categories: list[str] = Field(default_factory=list)
    agent_instructions: str | None = None
    category_instructions: str | None = None
    description: str | None = None


class AgentProfileRegistry:
    """Registry managing agent profiles, prompt template rendering, and output schemas.

    Covers R14.1 and R14.2.
    Satisfies ContextBuilder's InstructionProvider protocol:
        get_instructions(category: str) -> tuple[str, str]
    """

    def __init__(
        self,
        profiles: list[AgentProfile] | None = None,
        default_profile: str = "general_inquiry",
        template_base_dir: Path | str | None = None,
        schema_base_dir: Path | str | None = None,
    ) -> None:
        self.default_profile = default_profile
        self.template_base_dir = Path(template_base_dir) if template_base_dir else Path.cwd()
        self.schema_base_dir = Path(schema_base_dir) if schema_base_dir else Path.cwd()

        self._profiles: dict[str, AgentProfile] = {}
        self._category_map: dict[str, str] = {}
        self._schema_cache: dict[str, dict[str, Any]] = {}

        self._jinja_env = Environment(
            loader=FileSystemLoader(self.template_base_dir),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

        if profiles:
            for prof in profiles:
                self.register(prof)

    @property
    def profiles(self) -> list[AgentProfile]:
        """Return list of all registered profiles."""
        return list(self._profiles.values())

    def register(
        self,
        profile: AgentProfile,
        categories: Sequence[str] | None = None,
    ) -> None:
        """Register an agent profile and map its categories."""
        self._profiles[profile.profile] = profile

        cats_to_map = list(categories) if categories is not None else list(profile.categories)
        if not cats_to_map and profile.profile not in self._category_map.values():
            cats_to_map.append(profile.profile)

        for cat in cats_to_map:
            normalized = cat.strip().lower()
            self._category_map[normalized] = profile.profile

    def get_profile(self, name: str) -> AgentProfile | None:
        """Get profile by profile name."""
        return self._profiles.get(name)

    def resolve_profile(self, category: str | None) -> AgentProfile:
        """Resolve an AgentProfile from a classification category with fallback (R14.2).

        Args:
            category: Classification category (e.g. 'support', 'billing').

        Returns:
            Matched AgentProfile, or fallback default_profile if unmapped.

        Raises:
            KeyError: If fallback profile cannot be found.
        """
        if category:
            key = category.strip().lower()
            prof_name = self._category_map.get(key)
            if prof_name and prof_name in self._profiles:
                return self._profiles[prof_name]

        # Fallback to configured default profile
        if self.default_profile in self._profiles:
            return self._profiles[self.default_profile]

        # If default_profile not explicitly found, return first available profile if any
        if self._profiles:
            return next(iter(self._profiles.values()))

        raise KeyError(
            f"No profile mapped for category {category!r} and default profile "
            f"{self.default_profile!r} is not registered."
        )

    def get_instructions(self, category: str) -> tuple[str, str]:
        """Satisfies InstructionProvider protocol for ContextBuilder (R14.1, R14.8).

        Returns:
            Tuple of (agent_instructions, category_instructions).
        """
        profile = self.resolve_profile(category)
        agent_instr = profile.agent_instructions or DEFAULT_ENTERPRISE_INSTRUCTIONS
        cat_instr = (
            profile.category_instructions
            or f"Provide helpful, domain-specific guidance for {profile.knowledge_domain} matters."
        )
        return agent_instr, cat_instr

    def get_schema(self, profile: AgentProfile | str) -> dict[str, Any]:
        """Load and return structured JSON schema for profile (R14.1)."""
        if isinstance(profile, str):
            resolved = self.get_profile(profile) or self.resolve_profile(profile)
            target = resolved.output_schema
        else:
            target = profile.output_schema

        if isinstance(target, dict):
            return target

        if target in self._schema_cache:
            return self._schema_cache[target]

        schema_file = Path(target)
        if not schema_file.is_absolute():
            schema_file = self.schema_base_dir / schema_file

        if not schema_file.is_file():
            raise FileNotFoundError(f"Output schema file not found: {schema_file}")

        data: dict[str, Any] = json.loads(schema_file.read_text(encoding="utf-8"))
        self._schema_cache[target] = data
        return data

    def render_prompt(
        self,
        profile: AgentProfile | str,
        context: ContextPackage | dict[str, Any],
    ) -> str:
        """Render prompt template with provided generation context (R14.1, R14.6).

        Args:
            profile: AgentProfile or profile name.
            context: ContextPackage entity or context dictionary.

        Returns:
            Rendered prompt string.
        """
        resolved_profile = (
            self.get_profile(profile) or self.resolve_profile(profile)
            if isinstance(profile, str)
            else profile
        )

        template_path_str = resolved_profile.prompt_template
        # Normalize template relative path to template_base_dir
        rel_template_path = Path(template_path_str)
        with contextlib.suppress(ValueError):
            rel_template_path = rel_template_path.relative_to(self.template_base_dir)

        template = self._jinja_env.get_template(str(rel_template_path))

        # Prepare context dict
        if hasattr(context, "get_ordered_sections"):
            # ContextPackage instance
            template_vars: dict[str, Any] = {
                "agent_instructions": (
                    context.agent_instructions
                    or resolved_profile.agent_instructions
                    or DEFAULT_ENTERPRISE_INSTRUCTIONS
                ),
                "category_instructions": (
                    context.category_instructions or resolved_profile.category_instructions or ""
                ),
                "current_message": context.current_message,
                "thread_summary": context.thread_summary,
                "recent_messages": context.recent_messages,
                "retrieved_chunks": context.retrieved_chunks,
                "business_data": context.business_data,
                "profile": resolved_profile,
            }
        elif isinstance(context, dict):
            template_vars = dict(context)
            if "agent_instructions" not in template_vars:
                template_vars["agent_instructions"] = (
                    resolved_profile.agent_instructions or DEFAULT_ENTERPRISE_INSTRUCTIONS
                )
            if "category_instructions" not in template_vars:
                template_vars["category_instructions"] = (
                    resolved_profile.category_instructions or ""
                )
            template_vars.setdefault("profile", resolved_profile)
        else:
            template_vars = {"context": context, "profile": resolved_profile}

        return template.render(**template_vars)

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        template_base_dir: Path | str | None = None,
        schema_base_dir: Path | str | None = None,
    ) -> AgentProfileRegistry:
        """Construct registry from parsed configuration dictionary."""
        default_prof = str(data.get("default_profile", "general_inquiry"))
        raw_profiles = data.get("profiles", {})

        profiles_list: list[AgentProfile] = []
        if isinstance(raw_profiles, dict):
            for name, p_data in raw_profiles.items():
                p_data_copy = dict(p_data)
                p_data_copy.setdefault("profile", name)
                profiles_list.append(AgentProfile.model_validate(p_data_copy))
        elif isinstance(raw_profiles, list):
            for p_data in raw_profiles:
                profiles_list.append(AgentProfile.model_validate(p_data))

        return cls(
            profiles=profiles_list,
            default_profile=default_prof,
            template_base_dir=template_base_dir,
            schema_base_dir=schema_base_dir,
        )

    @classmethod
    def from_yaml(
        cls,
        path: Path | str,
        template_base_dir: Path | str | None = None,
        schema_base_dir: Path | str | None = None,
    ) -> AgentProfileRegistry:
        """Construct registry by loading a YAML configuration file."""
        config_file = Path(path)
        if not config_file.is_file():
            raise FileNotFoundError(f"Profile configuration file not found: {config_file}")

        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        return cls.from_dict(
            data=data or {},
            template_base_dir=template_base_dir,
            schema_base_dir=schema_base_dir,
        )
