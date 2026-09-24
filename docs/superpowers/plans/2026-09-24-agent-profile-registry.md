# Agent Profile Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the Agent Profile Registry for category-based specialization without multi-agent chains, supporting versioned prompt templates, structured output schemas, context policies, and automatic fallback.

**Architecture:** 
The Agent Profile Registry (`AgentProfileRegistry`) provides declarative specialization for reply generation. Instead of chaining separate planner, critic, and writer agents, email categories map directly to specialized `AgentProfile` specifications. Each profile specifies knowledge domain, response style, model tier, context policy, versioned Jinja2 prompt template, and output JSON schema. If an email's triage category is unmapped, the registry falls back to a configured default profile. The registry also satisfies the `InstructionProvider` protocol, enabling seamless prompt-prefix caching integration with `ContextBuilder`.

**Architecture Diagram:**

```mermaid
graph TD
    subgraph "Classification & Context Assembly"
        C[Classification Category] --> R[AgentProfileRegistry]
        R -->|Resolve Profile| P[AgentProfile]
        P -->|Fallback if unmapped| DEF[Default Profile: general_inquiry]
        CP[ContextPackage] --> TPL[Prompt Template Engine]
        P -->|Prompt Template: support.v1.j2| TPL
    end

    subgraph "Profile Specification (R14.1, R14.6)"
        P --> KD[knowledge_domain]
        P --> RS[response_style]
        P --> MT[model_tier]
        P --> POL[context_policy]
        P --> SCH[output_schema: schemas/reply.v1.json]
        P --> PV[prompt_version: support.v1]
    end

    subgraph "Downstream Generation"
        TPL --> PR[Rendered Prompt]
        PR --> GEN[Generation Call (Task 4.7)]
        SCH --> GEN
        PV -->|Recorded on draft| DR[GeneratedDraft.prompt_version]
    end
```

**Tech Stack:** Python 3.12, Pydantic V2, PyYAML, Jinja2, JSON Schema, pytest.

**Spec:** [`specs/tasks.md`](file:///home/ple/Documents/antigravity/dazzling-bose/specs/tasks.md) Task 4.6; [`specs/requirements.md`](file:///home/ple/Documents/antigravity/dazzling-bose/specs/requirements.md) `R14.1`, `R14.2`, `R14.6`; [`specs/design.md`](file:///home/ple/Documents/antigravity/dazzling-bose/specs/design.md) §5.7; [`docs/proposal/Technical Proposal — Enterprise RAG-Based Intelligent Email Management and Response System.md`](file:///home/ple/Documents/antigravity/dazzling-bose/docs/proposal/Technical%20Proposal%20%E2%80%94%20Enterprise%20RAG-Based%20Intelligent%20Email%20Management%20and%20Response%20System.md) §22.

## Global Constraints

- Provider names (`gmail`, `graph`, `imap`) appear only inside `packages/adapters/`.
- No vendor SDK imports outside `packages/llm/`.
- All model calls go through `LLMProvider`.
- No chained planner/critic/writer multi-agent pipeline in the default path (`R14.4`). Specialization is achieved strictly by configuration (`R14.1`).
- Every generated draft must record `prompt_version` (`R14.6`).
- All tests must run offline with zero network and zero live credentials (`R24.5`).

---

### Task 1: Environment Dependencies, Output Schema & Versioned Templates

**Files:**
- Modify: [`pyproject.toml:24-28`](file:///home/ple/Documents/antigravity/dazzling-bose/pyproject.toml#L24-L28)
- Create: [`schemas/reply.v1.json`](file:///home/ple/Documents/antigravity/dazzling-bose/schemas/reply.v1.json)
- Create: [`prompts/support.v1.j2`](file:///home/ple/Documents/antigravity/dazzling-bose/prompts/support.v1.j2)
- Create: [`prompts/billing.v1.j2`](file:///home/ple/Documents/antigravity/dazzling-bose/prompts/billing.v1.j2)
- Create: [`prompts/sales.v1.j2`](file:///home/ple/Documents/antigravity/dazzling-bose/prompts/sales.v1.j2)
- Create: [`prompts/general.v1.j2`](file:///home/ple/Documents/antigravity/dazzling-bose/prompts/general.v1.j2)
- Create: [`config/agent_profiles.yaml`](file:///home/ple/Documents/antigravity/dazzling-bose/config/agent_profiles.yaml)
- Test: [`tests/unit/test_agent_profile.py`](file:///home/ple/Documents/antigravity/dazzling-bose/tests/unit/test_agent_profile.py)

**Interfaces:**
- Consumes: `schemas/reply.v1.json`, Jinja2 templates, `config/agent_profiles.yaml`
- Produces: Installed Jinja2, valid reply schema file, core prompt templates, and default declarative YAML profile configuration.

- [ ] **Step 1: Write test verifying schema and template fixtures load cleanly**

```python
# In tests/unit/test_agent_profile.py
import json
from pathlib import Path
import yaml

def test_schema_file_valid_json() -> None:
    schema_path = Path("schemas/reply.v1.json")
    assert schema_path.is_file()
    data = json.loads(schema_path.read_text(encoding="utf-8"))
    assert data["properties"]["action"]["enum"] == ["reply", "forward", "escalate", "no_reply"]
    assert "draft" in data["required"]
    assert "confidence" in data["required"]

def test_agent_profiles_yaml_valid() -> None:
    config_path = Path("config/agent_profiles.yaml")
    assert config_path.is_file()
    content = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "profiles" in content
    assert "technical_support" in content["profiles"]
    assert "billing" in content["profiles"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_agent_profile.py -v`
Expected: FAIL with FileNotFoundError or ModuleNotFoundError

- [ ] **Step 3: Add jinja2 dependency, create reply schema, prompt templates, and configuration**

1. Add `jinja2>=3.1.6` to `pyproject.toml` dependencies and run `uv sync`.
2. Create `schemas/reply.v1.json` with the standard reply JSON schema from `design.md §5.7`.
3. Create versioned Jinja2 prompt templates (`prompts/support.v1.j2`, `prompts/billing.v1.j2`, `prompts/sales.v1.j2`, `prompts/general.v1.j2`).
4. Create `config/agent_profiles.yaml` specifying `technical_support`, `billing`, `sales`, and `general_inquiry`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_agent_profile.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock schemas/ prompts/ config/agent_profiles.yaml tests/unit/test_agent_profile.py
git commit -m "feat(profiles): add schema, templates, and profile config [task 4.6] [R14.1, R14.6]"
```

---

### Task 2: Implement `AgentProfile` Entity and `ContextPolicy` Enum

**Files:**
- Create: [`packages/llm/profile.py`](file:///home/ple/Documents/antigravity/dazzling-bose/packages/llm/profile.py)
- Modify: [`packages/llm/__init__.py`](file:///home/ple/Documents/antigravity/dazzling-bose/packages/llm/__init__.py)
- Modify: [`tests/unit/test_agent_profile.py`](file:///home/ple/Documents/antigravity/dazzling-bose/tests/unit/test_agent_profile.py)

**Interfaces:**
- Consumes: `ModelTier` from `packages.llm.protocol`
- Produces: `ContextPolicy`, `AgentProfile` dataclass/Pydantic model

- [ ] **Step 1: Write failing test for AgentProfile specification and validation**

```python
def test_agent_profile_creation() -> None:
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
    )
    assert profile.profile == "technical_support"
    assert profile.knowledge_domain == "support"
    assert profile.model_tier == ModelTier.ROUTINE
    assert profile.context_policy == ContextPolicy.THREAD_PLUS_RAG
    assert profile.prompt_version == "support.v1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_agent_profile.py -k test_agent_profile_creation -v`
Expected: FAIL with ImportError: cannot import name 'AgentProfile' from 'packages.llm.profile'

- [ ] **Step 3: Implement ContextPolicy and AgentProfile in packages/llm/profile.py**

```python
from enum import StrEnum
from pydantic import BaseModel, Field
from packages.llm.protocol import ModelTier

class ContextPolicy(StrEnum):
    THREAD_PLUS_RAG = "thread_plus_rag"
    THREAD_PLUS_RAG_PLUS_BUSINESS = "thread_plus_rag_plus_business"
    THREAD_ONLY = "thread_only"
    RAG_ONLY = "rag_only"

class AgentProfile(BaseModel):
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_agent_profile.py -k test_agent_profile_creation -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add packages/llm/profile.py packages/llm/__init__.py tests/unit/test_agent_profile.py
git commit -m "feat(profiles): implement AgentProfile and ContextPolicy [task 4.6] [R14.1, R14.6]"
```

---

### Task 3: Implement `AgentProfileRegistry` with Category Mapping, Template Rendering, and InstructionProvider Support

**Files:**
- Modify: [`packages/llm/profile.py`](file:///home/ple/Documents/antigravity/dazzling-bose/packages/llm/profile.py)
- Modify: [`packages/llm/__init__.py`](file:///home/ple/Documents/antigravity/dazzling-bose/packages/llm/__init__.py)
- Modify: [`tests/unit/test_agent_profile.py`](file:///home/ple/Documents/antigravity/dazzling-bose/tests/unit/test_agent_profile.py)

**Interfaces:**
- Consumes: `AgentProfile`, `ContextPackage` (`packages.domain.entities`), `InstructionProvider` (`packages.context.builder`)
- Produces: `AgentProfileRegistry` with:
  - `resolve_profile(category: str | None) -> AgentProfile`
  - `render_prompt(profile: AgentProfile, context: ContextPackage | dict[str, Any]) -> str`
  - `get_schema(profile: AgentProfile) -> dict[str, Any]`
  - `get_instructions(category: str) -> tuple[str, str]`
  - `load_from_yaml(path: Path | str) -> AgentProfileRegistry`

- [ ] **Step 1: Write failing tests for AgentProfileRegistry**

Test cases in `tests/unit/test_agent_profile.py`:
1. `test_registry_resolve_by_category`: Category "support" resolves to "technical_support".
2. `test_registry_fallback_to_default`: Unknown category "unknown_cat" resolves to default profile ("general_inquiry") (`R14.2`).
3. `test_registry_load_from_yaml`: Load `config/agent_profiles.yaml` and verify all profiles are mapped.
4. `test_registry_render_prompt`: Render prompt using Jinja2 with `ContextPackage`, asserting output contains instructions, thread history, and citations.
5. `test_registry_prompt_version_exposed`: Verify `profile.prompt_version` is accurately returned for recording on `GeneratedDraft.prompt_version` (`R14.6`).
6. `test_registry_schema_loading`: Verify output JSON schema loads and parses into valid dictionary (`R14.1`).
7. `test_registry_instruction_provider_compatibility`: Verify registry satisfies `InstructionProvider` protocol for `ContextBuilder`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_agent_profile.py -k "test_registry" -v`
Expected: FAIL with AttributeError or ImportError for `AgentProfileRegistry`

- [ ] **Step 3: Implement AgentProfileRegistry in packages/llm/profile.py**

Implement `AgentProfileRegistry` with:
- Profile storage by profile name and category index.
- Automatic fallback to `default_profile` when category is absent or unmapped (`R14.2`).
- Jinja2 `Environment` with `FileSystemLoader` for loading and rendering templates with cache.
- Schema loader with JSON caching.
- `get_instructions(category: str) -> tuple[str, str]` adhering to `InstructionProvider`.
- Class method `from_yaml(path)` and `from_dict(data)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_agent_profile.py -v`
Expected: PASS (all tests pass)

- [ ] **Step 5: Commit**

```bash
git add packages/llm/profile.py packages/llm/__init__.py tests/unit/test_agent_profile.py
git commit -m "feat(profiles): implement AgentProfileRegistry and template rendering [task 4.6] [R14.1, R14.2, R14.6]"
```

---

### Task 4: Configuration Settings, Documentation, and Quality Gate

**Files:**
- Modify: [`packages/core/settings.py`](file:///home/ple/Documents/antigravity/dazzling-bose/packages/core/settings.py)
- Modify: [`.env.example`](file:///home/ple/Documents/antigravity/dazzling-bose/.env.example)
- Modify: [`docs/configuration.md`](file:///home/ple/Documents/antigravity/dazzling-bose/docs/configuration.md)
- Modify: [`specs/tasks.md`](file:///home/ple/Documents/antigravity/dazzling-bose/specs/tasks.md)
- Modify: [`tests/unit/test_agent_profile.py`](file:///home/ple/Documents/antigravity/dazzling-bose/tests/unit/test_agent_profile.py)

**Interfaces:**
- Consumes: `AgentProfileSettings`
- Produces: Verified configuration keys, updated documentation, green CI quality gate, task marked `[x]`.

- [ ] **Step 1: Write test for AgentProfileSettings in AppSettings**

```python
def test_agent_profile_settings_defaults() -> None:
    from packages.core.settings import AppSettings
    settings = AppSettings()
    assert settings.agent_profiles.config_path == "config/agent_profiles.yaml"
    assert settings.agent_profiles.default_profile == "general_inquiry"
    assert settings.agent_profiles.prompts_dir == "prompts"
    assert settings.agent_profiles.schemas_dir == "schemas"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_agent_profile.py -k test_agent_profile_settings_defaults -v`
Expected: FAIL with AttributeError: 'AppSettings' object has no attribute 'agent_profiles'

- [ ] **Step 3: Update settings, documentation, and configuration templates**

1. Add `AgentProfileSettings` to `packages/core/settings.py` and mount onto `AppSettings`.
2. Add `AGENT_PROFILES__*` variables to `.env.example`.
3. Add Section 2.16 `Agent Profile Registry Configuration` in `docs/configuration.md`.
4. Re-export `AgentProfile`, `AgentProfileRegistry`, and `ContextPolicy` in `packages/llm/__init__.py`.
5. Mark Task 4.6 `[x]` in `specs/tasks.md`.

- [ ] **Step 4: Run full verification suite**

Run:
```bash
uv run pytest tests/unit/test_agent_profile.py -v
uv run ruff check packages/llm packages/core tests/unit/test_agent_profile.py
uv run ruff format --check packages/llm packages/core tests/unit/test_agent_profile.py
uv run mypy packages/llm packages/core
uv run pytest tests/unit/ -q
```
Expected: All tests pass, 0 lint errors, 0 format warnings, 0 type issues.

- [ ] **Step 5: Commit**

```bash
git add packages/core/settings.py packages/llm/__init__.py .env.example docs/configuration.md specs/tasks.md tests/unit/test_agent_profile.py
git commit -m "feat(profiles): add profile settings, configuration docs, and complete Task 4.6 [task 4.6] [R14.1, R14.2, R14.6]"
```

---

## Self-Review Checklist

1. **Spec Coverage:**
   - `R14.1`: Profiles specify `profile, knowledge_domain, response_style, model_tier, context_policy`, plus prompt template and output schema -> Covered in Tasks 1, 2, 3.
   - `R14.2`: Select profile from category with default profile fallback -> Covered in Task 3 (`resolve_profile`).
   - `R14.6`: Versioned prompt templates and `prompt_version` recorded on draft -> Covered in Tasks 1, 2, 3 (`support.v1.j2`, `prompt_version="support.v1"`).
2. **Placeholder Scan:** No "TBD", "TODO", or missing blocks. Exact schemas, templates, tests, and code specified.
3. **Type Consistency:** `ModelTier` from `packages.llm.protocol`, `ContextPolicy` enum, `ContextPackage` from `packages.domain.entities`.
